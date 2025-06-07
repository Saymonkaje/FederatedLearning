#include <boost/asio.hpp>
#include <boost/endian.hpp>
#include <boost/regex.hpp>
#include <iostream>
#include <fstream>
#include <thread>
#include <vector>
#include <mutex>
#include <chrono>
#include <filesystem>
#include <condition_variable>
#include <queue>
#include <boost/asio/ip/udp.hpp>
#include <atomic>

using boost::asio::ip::tcp;
using namespace std;
namespace fs = std::filesystem;

using NetworkSize = boost::endian::big_uint64_t;


const std::string AGGREGATION_FILE_DIRECTORY = "aggregation_models/";
const std::string GLOBAL_MODEL_DIRECTORY = "global_model/";
const std::string BASE_MODEL_FILE_DIRECTORY = "base_model/";
const std::string base_model_filename = "big_global_model_weights.ckpt";
const int PORT = 2121;
const std::string PYTHON_SERVER_IP = "127.0.0.1";
const int PYTHON_SERVER_PORT = 12345;
const int DISCOVERY_PORT = 2122;
const std::string DISCOVERY_REQUEST = "ANDROID_CLIENT_DISCOVERY";
const std::string DISCOVERY_RESPONSE = "SERVER_FOUND";

class Server {
public:
    Server(boost::asio::io_context& io_context, int port, int buffer_size)
        : acceptor_(io_context, tcp::endpoint(tcp::v4(), port)),
        buffer_size_(buffer_size),
        discovery_socket_(io_context, boost::asio::ip::udp::endpoint(boost::asio::ip::udp::v4(), DISCOVERY_PORT)) {
        std::cout << "Server is running on port " << port << " with buffer size " << buffer_size_ << std::endl;
        std::cout << "Discovery service running on UDP port " << DISCOVERY_PORT << std::endl;

        // Запускаємо службу виявлення в окремому потоці
        std::thread(&Server::run_discovery_service, this).detach();

    }

    void start_accepting() {
        while (true) {
            tcp::socket socket(io_context_);
            acceptor_.accept(socket);
            std::thread(&Server::handle_client, this, std::move(socket)).detach();
        }
    }

    void send_retrain_command() {
        std::lock_guard<std::mutex> lock(client_mutex_);
        if (clients_.size() <= 0)
        {
            return;
        }
        cout<< "Clients count: " << clients_.size() << endl;
        for (auto it = clients_.begin(); it != clients_.end();) {
            try {
                boost::asio::write(*it, boost::asio::buffer("RETRAIN\n"));
                tcp::socket training_client_sock = std::move(*it);
                it = clients_.erase(it);
                std::thread(&Server::receive_file_from_client, this, std::move(training_client_sock)).detach();
            }
            catch (std::exception& e) {
                std::cerr << "Error sending RETRAIN: " << e.what() << '\n';
                it = clients_.erase(it);
            }
        }
    }


    void aggregate_and_send_models() {
        string aggregated_dir = AGGREGATION_FILE_DIRECTORY;
        while (true) {
            std::this_thread::sleep_for(std::chrono::seconds(5));

            std::unique_lock<std::mutex> lock(waiting_client_mutes);
            if (clients_waiting_for_model.size() >= buffer_size_) {
                std::cout << "Enough clients waiting for model. Triggering aggregation..." << std::endl;

                tcp::socket python_socket(io_context_);
                try {
                    python_socket.connect(tcp::endpoint(boost::asio::ip::address::from_string(PYTHON_SERVER_IP), PYTHON_SERVER_PORT));
                    std::cout << "Connected to Python server." << std::endl;

                    char response[1024];
                    size_t len = python_socket.read_some(boost::asio::buffer(response));
                    std::string python_response(response, len);
                    std::cout << "Received from Python server: " << python_response << std::endl;


                    if (python_response.find("Script execution completed.") != std::string::npos) {
                        size_t filename_start = python_response.find("Filename: ");
                        if (filename_start != std::string::npos) {
                            std::string new_model_name = python_response.substr(filename_start + 10);  // "Filename: " has size of 10
                            new_model_name.erase(new_model_name.find_last_not_of("\r\n") + 1);
                            std::cout << "New model name: " << new_model_name << std::endl;

                            for (auto& client_socket : clients_waiting_for_model) {
                                try {
                                    send_file(client_socket, GLOBAL_MODEL_DIRECTORY + new_model_name);
                                    std::lock_guard<std::mutex> lock(client_mutex_);
                                    clients_.emplace_back(move(client_socket));

                                    std::cout << "Model sent to client." << std::endl;
                                }
                                catch (std::exception& e) {
                                    std::cerr << "Error sending model to client: " << e.what() << std::endl;
                                }
                            }
                            clients_waiting_for_model.clear();
                            buffer_cv_.notify_all();  // Сповіщаємо всі потоки, що буфер звільнився
                            cout<<"Notified all threads, that buffer is free"<<endl;


                        }
                        else {
                            std::cerr << "Filename not found in Python server response." << std::endl;
                        }
                    }

                }
                catch (std::exception& e) {
                    std::cerr << "Error connecting to Python server: " << e.what() << std::endl;
                }
            }
        }
    }

private:
    static std::atomic<int> next_client_id;

    struct QueuedClient {
        int id;
        tcp::socket socket;

        QueuedClient(tcp::socket&& sock)
            : id(next_client_id++), socket(std::move(sock)) {}
    };

    struct ClientQueue {
        std::queue<QueuedClient> queue;
        std::mutex mutex;
        std::condition_variable cv;
        bool is_processing = false;
    };

    ClientQueue client_queue;

    void run_discovery_service() {
        while (true) {
            try {
                // Буфер для отримання даних
                std::array<char, 1024> recv_buffer;

                // Кінцева точка для зберігання адреси відправника
                boost::asio::ip::udp::endpoint sender_endpoint;

                // Синхронно (блокуюче) очікуємо на отримання дейтаграми
                size_t bytes_recvd = discovery_socket_.receive_from(
                    boost::asio::buffer(recv_buffer), sender_endpoint);

                if (bytes_recvd > 0) {
                    std::string received_msg(recv_buffer.data(), bytes_recvd);

                    // Перевіряємо, чи це запит на виявлення
                    if (received_msg == DISCOVERY_REQUEST) {
                        std::cout << "Discovery request from: " << sender_endpoint.address().to_string() << std::endl;

                        // Готуємо та відправляємо відповідь
                        std::string response = DISCOVERY_RESPONSE + ":" + std::to_string(PORT);

                        boost::system::error_code ec;
                        discovery_socket_.send_to(
                            boost::asio::buffer(response), sender_endpoint, 0, ec);

                        if (ec) {
                            std::cerr << "Discovery response error: " << ec.message() << std::endl;
                        }
                    }
                }
            }
            catch (const std::exception& e) {
                std::cerr << "Discovery service error: " << e.what() << std::endl;
                // Цикл продовжується, що дозволяє відновити роботу після помилки
            }
        }
    }

    static void send_error(tcp::socket& socket, const std::string& message) {
        boost::asio::write(socket, boost::asio::buffer(message));
    }

    static void send_ok(tcp::socket& socket) {
        std::string message = "OK\n";
        boost::asio::write(socket, boost::asio::buffer(message));
    }


    static bool save_file(tcp::socket& socket) {
        std::string filename = "data.txt";

        std::ofstream file(filename, std::ios::binary);
        if (!file) {
            std::cerr << "Error opening file for writing: " << filename << std::endl;
            return false;
        }

        send_ok(socket);

        boost::asio::streambuf buffer;
        boost::system::error_code error;
        std::istream input_stream(&buffer);

        std::string line;
        size_t total_bytes_received = 0;

        string last_read_msg = "";
        while (boost::asio::read_until(socket, buffer, boost::regex("\n"), error)) {
            std::getline(input_stream, line);
            file << line << "\n";
            total_bytes_received += line.size() + 1;
        }

        cout << "last read msg = " << line << endl;
        std::cout << "[Server] received bytes: " << total_bytes_received << std::endl;
        std::cout << "Data received and saved to file: " << filename << std::endl;
        return true;
    }

    void handle_client(tcp::socket socket) {
        try {
            while (true) {
                char data[1024];
                boost::system::error_code error;
                size_t length = socket.read_some(boost::asio::buffer(data), error);
                if (error == boost::asio::error::eof) break;

                std::string command(data, length);
                std::istringstream iss(command);
                std::string cmd;
                iss >> cmd;

                if (cmd == "SEND_BASE_MODEL") {
                    std::string filepath = BASE_MODEL_FILE_DIRECTORY + base_model_filename;
                    send_file(socket, filepath);
                }
                else if (cmd == "SEND_MIN_VALS_FILE") {
                    std::string filepath = BASE_MODEL_FILE_DIRECTORY + "min_vals.txt";
                    if (fs::exists(filepath)) {
                        send_file(socket, filepath);
                    }
                    else {
                        std::cerr << "File not found: " << filepath << std::endl;
                        boost::asio::write(socket, boost::asio::buffer("ERROR_FILE_NOT_FOUND\n"), error);
                    }
                }
                else if (cmd == "SEND_MAX_VALS_FILE") {
                    std::string filepath = BASE_MODEL_FILE_DIRECTORY + "max_vals.txt";
                    if (fs::exists(filepath)) {
                        send_file(socket, filepath);
                    }
                    else {
                        std::cerr << "File not found: " << filepath << std::endl;
                        boost::asio::write(socket, boost::asio::buffer("ERROR_FILE_NOT_FOUND\n"), error);
                    }
                }
                else if (cmd == "UPLOAD_DATA") {
                    save_file(socket);
                }
                if (cmd == "LISTEN_COMMANDS") {
                    std::lock_guard<std::mutex> lock(client_mutex_);
                    clients_.emplace_back(std::move(socket));
                    return;
                }
            }
        }
        catch (std::exception& e) {
            std::cerr << "Client error: " << e.what() << '\n';
        }
    }

    std::string read_line_from_socket(tcp::socket& socket, boost::system::error_code& error) {
        boost::asio::streambuf streambuf;
        boost::asio::read_until(socket, streambuf, '\n', error);
        if (error) {
            return "";
        }
        std::istream is(&streambuf);
        std::string line;
        std::getline(is, line);
        if (!line.empty() && line.back() == '\r') {
            line.pop_back();
        }
        streambuf.consume(streambuf.size());
        return line;
    }


    void receive_file_from_client(tcp::socket socket) {
        boost::system::error_code error;

        std::string command = read_line_from_socket(socket, error);
        if (error) {
            cerr << "Error receiving command: " << error.message() << '\n';
            return;
        }

        bool is_weights = false;
        if (command == "SEND_MODEL") {
            cout << "Command SEND_MODEL received.\n";
            is_weights = false;
        }
        else if (command == "SEND_WEIGHTS") {
            cout << "Command SEND_WEIGHTS received.\n";
            is_weights = true;
        }
        else {
            cerr << "Unexpected command: " << command << '\n';
            return;
        }

        // Додаємо клієнта до черги
        {
            std::unique_lock<std::mutex> queue_lock(client_queue.mutex);
            client_queue.queue.emplace(std::move(socket));
            std::cout << "Client #" << client_queue.queue.back().id << " added to queue. Queue size: " << client_queue.queue.size() << std::endl;
        }

        // Чекаємо нашої черги
        std::unique_lock<std::mutex> queue_lock(client_queue.mutex);
        int waiting_client_id = client_queue.queue.front().id;
        cout << "Waiting for client #" << waiting_client_id << " to be processed" << endl;

        client_queue.cv.wait(queue_lock, [this] {
            return !client_queue.is_processing && client_queue.queue.front().socket.is_open();
        });
        cout << "Client #" << waiting_client_id << " is being processed" << endl;

        client_queue.is_processing = true;
        QueuedClient current_client = std::move(client_queue.queue.front());
        client_queue.queue.pop();
        queue_lock.unlock();

        try {
            // Чекаємо, поки звільниться місце в буфері
            cout << "Waiting for empty buffer for client #" << current_client.id << endl;
            std::unique_lock<std::mutex> buffer_lock(waiting_client_mutes);
            buffer_cv_.wait(buffer_lock, [this] {
                return clients_waiting_for_model.size() < buffer_size_;
            });
            cout << "Buffer is empty for client #" << current_client.id << endl;

            boost::asio::write(current_client.socket, boost::asio::buffer("READY\n"), error);
            if (error) {
                cerr << "Error sending READY: " << error.message() << '\n';
                return;
            }
            cout << "Sent READY.\n";

            std::string received_filename = read_line_from_socket(current_client.socket, error);
            if (error || received_filename.empty()) {
                cerr << "Error receiving filename or filename is empty: " << error.message() << '\n';
                return;
            }
            cout << "Received filename = " << received_filename << "\n";

            boost::asio::write(current_client.socket, boost::asio::buffer("OK\n"), error);
            if (error) {
                cerr << "Error sending OK: " << error.message() << '\n';
                return;
            }
            cout << "Sent OK.\n";


            std::string models_dir = AGGREGATION_FILE_DIRECTORY;
            try {
                std::filesystem::create_directories(models_dir);
            }
            catch (const std::filesystem::filesystem_error& e) {
                cerr << "Error creating directory " << models_dir << ": " << e.what() << '\n';
                return;
            }
            std::string filepath = models_dir + "/" + received_filename;
            std::ofstream file(filepath, std::ios::binary | std::ios::trunc);
            if (!file) {
                cerr << "Error opening file for writing: " << filepath << "\n";
                return;
            }
            cout << "File " << filepath << " opened for writing.\n";

            cout << "waiting for file size line\n";
            std::string file_size_line = read_line_from_socket(current_client.socket, error);
            cout << "Received file size line: '" << file_size_line << "'\n";

            if (error) {
                cerr << "Error receiving file size line: " << error.message() << '\n';
                file.close();
                return;
            }

            long long expected_file_size = -1;
            std::string prefix = "FILE_SIZE:";
            if (file_size_line.rfind(prefix, 0) == 0) {
                try {
                    expected_file_size = std::stoll(file_size_line.substr(prefix.length()));
                }
                catch (const std::exception& e) {
                    cerr << "Error parsing file size from '" << file_size_line << "': " << e.what() << '\n';
                    file.close();
                    return;
                }
            }
            else {
                cerr << "Unexpected format for file size. Got: " << file_size_line << '\n';
                file.close();
                return;
            }

            if (expected_file_size < 0) {
                cerr << "Invalid file size received: " << expected_file_size << '\n';
                file.close();
                return;
            }
            cout << "Expecting file size: " << expected_file_size << " bytes.\n";

            // 5.
            boost::asio::write(current_client.socket, boost::asio::buffer("SIZE_RECEIVED\n"), error);
            if (error) {
                cerr << "Error sending SIZE_RECEIVED: " << error.message() << '\n';
                file.close();
                return;
            }
            cout << "Sent SIZE_RECEIVED.\n";

            std::vector<char> read_buffer(65536);
            long long total_bytes_received = 0;
            while (total_bytes_received < expected_file_size) {
                size_t bytes_to_read = std::min(static_cast<long long>(read_buffer.size()), expected_file_size - total_bytes_received);
                size_t len = current_client.socket.read_some(boost::asio::buffer(read_buffer.data(), bytes_to_read), error);

                if (error == boost::asio::error::eof) {
                    cerr << "EOF received prematurely. Expected " << expected_file_size << ", got " << total_bytes_received << ".\n";
                    file.close();
                    return;
                }
                if (error) {
                    cerr << "Error receiving file data: " << error.message() << '\n';
                    file.close();
                    return;
                }
                if (len == 0 && (expected_file_size - total_bytes_received > 0)) {
                    cerr << "Socket read 0 bytes prematurely. Expected " << expected_file_size << ", got " << total_bytes_received << ".\n";
                    file.close();
                    return;
                }

                file.write(read_buffer.data(), len);
                total_bytes_received += len;
            }
            file.close();

            if (total_bytes_received == expected_file_size) {
                cout << "File " << received_filename << " received successfully (" << total_bytes_received << " bytes).\n";
            }
            else {
                cerr << "File " << received_filename << " might be corrupted. Expected " << expected_file_size << ", got " << total_bytes_received << ".\n";
            }

            boost::asio::write(current_client.socket, boost::asio::buffer("OK\n"), error);
            if (error) {
                cerr << "Error sending OK: " << error.message() << '\n';
                return;
            }
            cout << "Sent OK.\n";

            std::string data_count_line = read_line_from_socket(current_client.socket, error);
            if (error) {
                cerr << "Error receiving data count line: " << error.message() << '\n';
                return;
            }

            std::string data_count_prefix = "DATA_COUNT:";
            if (data_count_line.rfind(data_count_prefix, 0) == 0) {
                std::string data_count_value = data_count_line.substr(data_count_prefix.length());
                cout << "Received data count: " << data_count_value << "\n";

                std::string base_filename = received_filename;
                size_t dot_pos = base_filename.rfind('.');
                if (dot_pos != std::string::npos) {
                    base_filename = base_filename.substr(0, dot_pos);
                }
                std::string data_count_filename = models_dir + "/" + base_filename + "_data_count.txt";

                std::ofstream data_count_file(data_count_filename);
                if (data_count_file) {
                    data_count_file << data_count_value;
                    data_count_file.close();
                    cout << "Data count saved to: " << data_count_filename << "\n";
                }
                else {
                    cerr << "Error saving data count to file: " << data_count_filename << "\n";
                }
            }
            else {
                cerr << "Invalid data count format received: " << data_count_line << "\n";
            }

            boost::asio::write(current_client.socket, boost::asio::buffer("DATA_COUNT_RECEIVED\n"), error);
            if (error) {
                cerr << "Error sending DATA_COUNT_RECEIVED: " << error.message() << '\n';
                return;
            }
            cout << "Sent DATA_COUNT_RECEIVED.\n";

            // Після успішної обробки додаємо до списку очікуючих
            clients_waiting_for_model.emplace_back(std::move(current_client.socket));
            std::cout << "File processing finished for client #" << current_client.id << ". Added to waiting list." << std::endl;
        }
        catch (const std::exception& e) {
            std::cerr << "Error processing client #" << current_client.id << ": " << e.what() << std::endl;
        }

        // Позначаємо, що обробка завершена
        {
            std::unique_lock<std::mutex> queue_lock(client_queue.mutex);
            client_queue.is_processing = false;
            if (!client_queue.queue.empty()) {
                client_queue.cv.notify_one();
            }
        }
    }


    static bool send_file(tcp::socket& socket, const std::string& filename) {
        std::ifstream file(filename, std::ios::binary | std::ios::ate);
        if (!file) {
            std::cerr << "File not found: " << filename << std::endl;
            return false;
        }

        std::streamsize size = file.tellg();
        file.seekg(0, std::ios::beg);
        std::cout << "File size: " << size << " bytes" << std::endl;

        boost::system::error_code error;

        std::string size_str = std::to_string(size) + "\n";
        boost::asio::write(socket, boost::asio::buffer(size_str), error);

        char response[1024];
        size_t len = socket.read_some(boost::asio::buffer(response), error);
        std::string response_str(response, len);
        if (response_str.find("OK") == std::string::npos) {
            std::cerr << "Don`t find ok." << std::endl;
            return false;
        }

        char buffer[4096];
        std::streamsize total_sent = 0;
        while (file.read(buffer, sizeof(buffer)) || file.gcount() > 0) {
            boost::asio::write(socket, boost::asio::buffer(buffer, file.gcount()), error);
            total_sent += file.gcount();
            std::cout << "Sent bytes: " << total_sent << std::endl;
        }

        std::cout << "File sent, total: " << total_sent << " bytes" << std::endl;
        return true;
    }


    boost::asio::io_context io_context_;
    tcp::acceptor acceptor_;
    std::vector<tcp::socket> clients_;
    std::vector<tcp::socket> clients_waiting_for_model;
    std::mutex waiting_client_mutes;
    std::mutex client_mutex_;
    std::condition_variable buffer_cv_;
    int buffer_size_;

    boost::asio::ip::udp::socket discovery_socket_;
    boost::asio::ip::udp::endpoint remote_endpoint_;
    std::array<char, 1024> discovery_recv_buffer_;
};

std::atomic<int> Server::next_client_id(1);

int main(int argc, char* argv[]) {
    try {
        // Перевіряємо аргументи командного рядка
        if (argc != 2) {
            std::cerr << "Usage: " << argv[0] << " <buffer_size>" << std::endl;
            return 1;
        }

        // Отримуємо розмір буфера з аргументів
        int buffer_size;
        try {
            buffer_size = std::stoi(argv[1]);
            if (buffer_size < 1) {
                throw std::invalid_argument("Buffer size must be positive");
            }
        } catch (const std::exception& e) {
            std::cerr << "Invalid buffer size: " << e.what() << std::endl;
            return 1;
        }

        boost::asio::io_context io_context;
        Server server(io_context, PORT, buffer_size);
        std::thread server_thread([&server]() { server.start_accepting(); });
        std::thread aggregation_thread([&server]() { server.aggregate_and_send_models(); });


        while (true) {
            this_thread::sleep_for(2000ms);
            server.send_retrain_command();
        }

        server_thread.join();
    }
    catch (const std::exception& e) {
        std::cerr << "Server error: " << e.what() << std::endl;
    }

    return 0;
}
