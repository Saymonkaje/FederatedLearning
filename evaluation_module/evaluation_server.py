# evaluation_server.py


import socket
import os
import time
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')  # Встановлюємо агресивний режим для роботи в неосновному потоці
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score
from util_functions import SignalPredictor, INPUT_SIZE, FEATURES, load_and_prepare_test_data
import threading
import queue
import struct

# Створюємо чергу для зберігання метрик
metrics_queue = queue.Queue()
# Змінна для зберігання IP адреси клієнта, який відкрив сервер
client_ip = None
client_ip_lock = threading.Lock()  # Для безпечної роботи з IP в різних потоках


def send_metrics_to_gui():
    """Функція для відправки метрик на GUI в окремому потоці"""
    while True:
        try:
            metrics = metrics_queue.get()
            if metrics is None:  # Сигнал для завершення потоку
                break


            metrics_json = json.dumps(metrics)
            try:
                with client_ip_lock:
                    current_client_ip = client_ip


                if current_client_ip is None:
                    print("Немає активного клієнта для відправки метрик")
                    metrics_queue.put(metrics)  # Повертаємо метрики в чергу
                    time.sleep(5)
                    continue


                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as gui_socket:
                    gui_socket.settimeout(5)
                    gui_socket.connect((current_client_ip, 54322))
                    gui_socket.sendall(metrics_json.encode())
                    print(f"Метрики надіслано на GUI ({current_client_ip}).")
            except Exception as e:
                print(f"Помилка надсилання метрик на GUI: {e}")
                # Додаємо метрики назад до черги для повторної спроби
                metrics_queue.put(metrics)
                time.sleep(5)  # Чекаємо перед повторною спробою
        except Exception as e:
            print(f"Помилка в потоці відправки метрик: {e}")
            time.sleep(5)


# Запускаємо потік для відправки метрик
metrics_thread = threading.Thread(target=send_metrics_to_gui, daemon=True)
metrics_thread.start()


def handle_discovery_requests(broadcast_port=54323):
    """Обробка broadcast-запитів пошуку сервера"""
    global client_ip
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', broadcast_port))


    print(f"Сервер оцінки очікує запити пошуку на порту {broadcast_port}")


    while True:
        try:
            data, addr = sock.recvfrom(1024)
            if data == b"EVALUATION_SERVER_DISCOVERY":
                client_ip = addr[0]  # Отримуємо IP адресу з кортежу
                print(f"Отримано запит пошуку від {addr}")


                sock.sendto(b"EVALUATION_SERVER_RESPONSE", addr)
                print(f"Відправлено відповідь на {addr}")
        except Exception as e:
            print(f"Помилка при обробці запиту пошуку: {e}")


def mean_absolute_percentage_error(y_true, y_pred):
    """Обчислення середньої абсолютної процентної помилки (MAPE)"""
    mask = y_true != 0
    mape = np.mean(np.abs((y_true[mask] - y_pred[mask]) / y_true[mask])) * 100
    return mape


def plot_predictions(y_true, y_pred, feature_names, model_name):
    """Візуалізація прогнозів для кожного параметра"""
    plt.figure(figsize=(15, 10))
    plt.suptitle(f'Прогнози моделі: {model_name}', fontsize=16)
    for i in range(FEATURES):
        plt.subplot(2, 3, i+1)
        plt.scatter(y_true[:, i], y_pred[:, i], alpha=0.5)
        plt.plot([y_true[:, i].min(), y_true[:, i].max()],
                [y_true[:, i].min(), y_true[:, i].max()],
                'r--', lw=2)
        plt.xlabel('Справжні значення')
        plt.ylabel('Прогнозовані значення')
        plt.title(f'Параметр {feature_names[i]}')
    plt.tight_layout()
    plt.savefig(f'testing_result/prediction_plots_{model_name}.png')
    plt.close()


def plot_error_distribution(y_true, y_pred, feature_names, model_name):
    """Візуалізація розподілу помилок"""
    errors = y_pred - y_true
    plt.figure(figsize=(15, 10))
    plt.suptitle(f'Розподіл помилок моделі: {model_name}', fontsize=16)
    for i in range(FEATURES):
        plt.subplot(2, 3, i+1)
        sns.histplot(errors[:, i], kde=True)
        plt.xlabel('Помилка')
        plt.title(f'Розподіл помилок для {feature_names[i]}')
    plt.tight_layout()
    plt.savefig(f'testing_result/error_distribution_{model_name}.png')
    plt.close()


def plot_time_series(y_true, y_pred, feature_names, model_name, window_size=150):
    """Візуалізація прогнозів у часовій області"""
    total_samples = len(y_true)
    start_indices = [0, total_samples // 2 - window_size // 2, total_samples - window_size - 100]
    section_names = ['Початок датасету', 'Середина датасету', 'Кінець датасету']


    for section_idx, (start_idx, section_name) in enumerate(zip(start_indices, section_names)):
        plt.figure(figsize=(15, 10))
        plt.suptitle(f'Часові ряди моделі: {model_name} ({section_name})', fontsize=16)
        for i in range(FEATURES):
            plt.subplot(2, 3, i+1)
            x = np.arange(start_idx, start_idx + window_size)
            plt.plot(x, y_true[start_idx:start_idx + window_size, i], 'b-',
                    label='Справжні значення', alpha=0.7)
            plt.plot(x, y_pred[start_idx:start_idx + window_size, i], 'r--',
                    label='Прогнозовані значення', alpha=0.7)
            plt.xlabel('Індекс виміру')
            plt.ylabel('Значення')
            plt.title(f'Параметр {feature_names[i]}')
            plt.legend()
            plt.grid(True)
        plt.tight_layout()
        plt.savefig(f'testing_result/time_series_plots_{model_name}_{section_idx+1}.png')
        plt.close()






def plot_kde_residuals(y_true, y_pred, feature_names, model_name):
    """Візуалізація KDE залишків"""
    residuals = y_pred - y_true
    plt.figure(figsize=(15, 10))
    plt.suptitle(f'KDE залишків моделі: {model_name}', fontsize=16)
    for i in range(FEATURES):
        plt.subplot(2, 3, i+1)
        sns.kdeplot(data=residuals[:, i], fill=True)
        plt.axvline(x=0, color='r', linestyle='--', alpha=0.5)
        plt.xlabel('Залишки')
        plt.ylabel('Щільність')
        plt.title(f'Параметр {feature_names[i]}')
    plt.tight_layout()
    plt.savefig(f'testing_result/kde_residuals_{model_name}.png')
    plt.close()


def evaluate_model(model, X_test, y_test, model_name):
    """Модифікована функція оцінки, що повертає лише метрики та зберігає графіки."""
    predictions = model.model.predict(X_test)


    # Метрики
    metrics = {
        'model_name': model_name,  # Додаємо ім'я моделі до метрик
        'MSE': mean_squared_error(y_test, predictions),
        'RMSE': np.sqrt(mean_squared_error(y_test, predictions)),
        'MAE': mean_absolute_error(y_test, predictions),
        'R²': r2_score(y_test, predictions),
        'MAPE': mean_absolute_percentage_error(y_test, predictions)
    }


    # Візуалізація результатів (зберігаємо файли)
    feature_names = ['AccX', 'AccY', 'AccZ', 'GyroX', 'GyroY', 'GyroZ']
    plot_predictions(y_test, predictions, feature_names, model_name)
    plot_error_distribution(y_test, predictions, feature_names, model_name)
    plot_time_series(y_test, predictions, feature_names, model_name)
    plot_kde_residuals(y_test, predictions, feature_names, model_name)


    return metrics


def evaluate_model_async(model_path, model_name):
    """Асинхронна оцінка моделі в окремому потоці"""
    try:
        print("Завантаження тестових даних...")
        X_test, y_test = load_and_prepare_test_data("./testing_data/merged_testing_data_12min.txt")

        model_to_evaluate = SignalPredictor()
        model_to_evaluate.restore(model_path)
        print("Модель завантажено, починається оцінка...")

        metrics = evaluate_model(model_to_evaluate, X_test, y_test, model_name)
        print("Оцінку завершено. Графіки збережено.")

        # Додаємо метрики до черги для відправки на GUI
        metrics_queue.put(metrics)

        # Прибираємо за собою
        try:
            os.remove(model_path)
        except Exception as e:
            print(f"Помилка видалення тимчасового файлу: {e}")

    except Exception as e:
        print(f"Помилка при асинхронній оцінці моделі: {e}")


def handle_evaluation_request(client_socket):
    """Обробляє запит на оцінку: отримує модель і запускає асинхронну оцінку"""
    try:
        # 1. Отримати назву файлу
        model_name_bytes = client_socket.recv(1024)
        if not model_name_bytes:
            print("Не отримано назву файлу")
            return

        model_name = model_name_bytes.decode().strip()
        print(f"Отримано запит на оцінку моделі: {model_name}")

        # Відправляємо підтвердження отримання назви файлу
        client_socket.sendall(b"FILENAME_RECEIVED")

        print("Sended confirmation for filename received")
        # Створюємо директорію для тимчасового збереження моделей
        os.makedirs("received_models", exist_ok=True)
        model_path = os.path.join("received_models", model_name)
        
        raw_size = client_socket.recv(8)
        if len(raw_size) < 8:
            print("Не вдалося отримати розмір моделі")
            return
        model_size = struct.unpack('>Q', raw_size)[0]
        print(f"Очікується отримати модель розміром {model_size} байт")


        # 2. Отримати і зберегти файл моделі
        received = 0
        with open(model_path, 'wb') as f:
            while received < model_size:
                try:
                    chunk = client_socket.recv(min(4096, model_size - received))
                    if not chunk:
                        print("Потік раптово обірвався")
                        return
                    f.write(chunk)
                    received += len(chunk)
                    print(f"Отримано {received}/{model_size} байт")
                except socket.timeout:
                    print("Таймаут при отриманні даних")
                    return
                except Exception as e:
                    print(f"Помилка при отриманні даних: {e}")
                    return


        print(f"Модель збережено в {model_path} (розмір: {received} байт)")
        
        # Запускаємо оцінку в окремому потоці
        evaluation_thread = threading.Thread(
            target=evaluate_model_async,
            args=(model_path, model_name),
            daemon=True
        )
        evaluation_thread.start()

        # Відправляємо підтвердження, що оцінка запущена
        client_socket.sendall(b"EVALUATION_STARTED")

    except Exception as e:
        print(f"Помилка під час обробки запиту: {e}")
        try:
            client_socket.sendall(b"ERROR_PROCESSING_REQUEST")
        except:
            pass
    finally:
        try:
            client_socket.close()
        except:
            pass


def start_evaluation_server(host='0.0.0.0', port=54321):
    """Основна функція запуску сервера."""
    os.makedirs('evaluation_results', exist_ok=True)
    os.makedirs('testing_result', exist_ok=True)


    # Запускаємо потік для обробки запитів пошуку
    discovery_thread = threading.Thread(target=handle_discovery_requests, daemon=True)
    discovery_thread.start()


    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((host, port))
    server_socket.listen(5)
    print(f"Сервер оцінки запущено на {host}:{port} і очікує на з'єднання...")
    print(f"Метрики будуть надсилатися на порт 54322 для GUI")


    try:
        while True:
            client_socket, addr = server_socket.accept()
            print(f"Прийнято з'єднання від {addr}")
            handler_thread = threading.Thread(target=handle_evaluation_request, args=(client_socket,))
            handler_thread.start()
    except KeyboardInterrupt:
        print("\nЗавершення роботи сервера...")
        # Сигналізуємо потоку метрик про завершення
        metrics_queue.put(None)
        metrics_thread.join()
    finally:
        server_socket.close()


if __name__ == "__main__":
    start_evaluation_server()
