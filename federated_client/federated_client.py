import os
import sys
import socket
import struct
import time
import argparse
import tensorflow as tf
import numpy as np
import glob

# Додаємо кореневу директорію проекту до PYTHONPATH
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core_ml_components.signal_predictor import SignalPredictor
from core_ml_components.util_functions import load_data, apply_moving_average, INPUT_SIZE, OUTPUT_SIZE, FEATURES

class FederatedClient:
    def __init__(self, server_host='localhost', server_port=2121, data_dir_num=1, max_rounds=10, local_epochs=5):
        self.server_host = server_host
        self.server_port = server_port
        self.socket = None
        self.data_iteration = 1
        self.client_dir = f"./client{data_dir_num}"
        self.base_model_path = os.path.join(self.client_dir, "base_model/signal_predictor_model.ckpt")
        self.data_dir = os.path.join(self.client_dir, "data")
        self.base_model_loaded = os.path.exists(self.base_model_path)
        self.data_dir_num = data_dir_num
        self.max_rounds = max_rounds
        self.current_round = 0
        self.local_epochs = local_epochs
        
        # Підраховуємо кількість доступних файлів даних
        self.available_data_files = sorted(glob.glob(os.path.join(self.data_dir, "data*.txt")))
        self.total_data_files = len(self.available_data_files)
        if self.total_data_files == 0:
            raise Exception("Не знайдено файлів даних")
        
        # Визначаємо раунд, з якого почнеться повторне використання даних
        self.data_reuse_start_round = self.total_data_files + 1
        if self.data_reuse_start_round <= self.max_rounds:
            print(f"Увага: Дані будуть повторно використовуватися починаючи з раунду {self.data_reuse_start_round}")

        # Створюємо необхідні директорії
        os.makedirs(self.client_dir, exist_ok=True)
        os.makedirs(os.path.join(self.client_dir, "base_model"), exist_ok=True)
        os.makedirs(os.path.join(self.client_dir, "retrained_model"), exist_ok=True)
        os.makedirs(os.path.join(self.client_dir, "received_model"), exist_ok=True)
        os.makedirs(self.data_dir, exist_ok=True)

        self.model = SignalPredictor()
        self.model.model.build(input_shape=(None, INPUT_SIZE, FEATURES))

        if self.base_model_loaded:
            self.model.restore(self.base_model_path)


    def connect_to_server(self):
        """Підключення до сервера та відправка команди LISTEN_COMMANDS"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1) # <--- Додати цей рядок
            self.socket.connect((self.server_host, self.server_port))
            print("Підключено до сервера")

            # Перевіряємо наявність базової моделі
            if not self.base_model_loaded:
                print("Базова модель не знайдена, завантажуємо...")
                if not self.download_base_model():
                    print("Не вдалося завантажити базову модель. Завершення роботи.")
                    self.socket.close()
                    return False
                self.base_model_loaded = True

            # Відправляємо команду LISTEN_COMMANDS
            self.socket.sendall(b"LISTEN_COMMANDS\n")
            print("Відправлено команду LISTEN_COMMANDS")

            return True
        except Exception as e:
            print(f"Помилка підключення до сервера: {e}")
            return False

    def download_base_model(self):
        """Завантаження базової моделі з сервера"""
        try:
            # Відправляємо запит на отримання базової моделі
            self.socket.sendall(b"SEND_BASE_MODEL\n")

            # Очікуємо розмір файлу
            response = self.socket.recv(1024).decode().strip()
            if not response.isdigit():
                raise Exception(f"Неочікувана відповідь сервера: {response}")

            file_size = int(response)
            self.socket.sendall(b"OK\n")

            # Створюємо директорію для моделі, якщо її немає
            os.makedirs(os.path.dirname(self.base_model_path), exist_ok=True)

            # Завантажуємо файл
            with open(self.base_model_path, 'wb') as f:
                bytes_received = 0
                while bytes_received < file_size:
                    chunk = self.socket.recv(min(4096, file_size - bytes_received))
                    if not chunk:
                        raise Exception("З'єднання перервано під час завантаження моделі")
                    f.write(chunk)
                    bytes_received += len(chunk)

            print(f"Базова модель завантажена: {self.base_model_path}")
            return True

        except Exception as e:
            print(f"Помилка завантаження базової моделі: {e}")
            return False

    def get_next_data_file(self):
        """Отримання наступного файлу даних для тренування"""
        if not self.available_data_files:
            raise Exception("Не знайдено файлів даних")

        if self.data_iteration > len(self.available_data_files):
            self.data_iteration = 1
            if self.current_round >= self.data_reuse_start_round:
                print(f"Data exhaustion with reuse (раунд {self.current_round + 1})")
            else:
                print(f"Local data exhaustion (раунд {self.current_round + 1})")

        data_file = self.available_data_files[self.data_iteration - 1]
        self.data_iteration += 1
        return data_file

    def retrain_model(self):
        """Перетренування моделі на локальних даних"""
        try:
            self.model.restore(self.base_model_path)
            print(f"Ваги моделі завантажені: {self.base_model_path}")

            # Завантажуємо дані для тренування
            data_file = self.get_next_data_file()
            print(f"Використовуємо дані з файлу: {data_file}")

            # Завантажуємо параметри нормалізації
            try:
                min_vals = np.load("common_data/min_vals.npy")
                max_vals = np.load("common_data/max_vals.npy")
                print("Завантажено параметри нормалізації")
            except FileNotFoundError:
                print("Параметри нормалізації не знайдено, будуть обчислені заново")
                min_vals = None
                max_vals = None

            # Завантажуємо дані
            train_X, train_Y = load_data(data_file, INPUT_SIZE, OUTPUT_SIZE, min_vals, max_vals)

            # Зберігаємо кількість навчальних прикладів для подальшого використання
            self.last_training_samples = len(train_X)

            # Параметри тренування
            BATCH_SIZE = 128
            EPOCHS = self.local_epochs  # Використовуємо задану кількість локальних епох

            print(f"Початок перетренування моделі... (локальні епохи: {EPOCHS})")
            for epoch in range(EPOCHS):
                epoch_losses = []
                # Створюємо батчі
                indices = np.random.permutation(len(train_X))
                for i in range(0, len(train_X), BATCH_SIZE):
                    batch_indices = indices[i:min(i + BATCH_SIZE, len(train_X))]
                    batch_X = train_X[batch_indices]
                    batch_y = train_Y[batch_indices]

                    # Тренуємо модель
                    train_result = self.model.train(x=batch_X, y=batch_y)
                    epoch_losses.append(train_result['loss'])

                avg_loss = np.mean(epoch_losses)
                print(f"Епоха {epoch + 1}/{EPOCHS}, Середня втрата: {avg_loss:.6f}")

            # Зберігаємо перетреновану модель
            checkpoint_path = os.path.join(self.client_dir, "retrained_model", f"model_client_{self.data_dir_num}.ckpt")
            self.model.save(checkpoint_path)

            print(f"Модель перетренована та збережена: {checkpoint_path}")
            return checkpoint_path

        except Exception as e:
            print(f"Помилка перетренування моделі: {e}")
            return None

    def send_model_to_server(self, model_path):
        """Відправка перетренованої моделі на сервер"""
        try:
            # Відправляємо команду SEND_MODEL
            self.socket.sendall(b"SEND_MODEL\n")

            # Очікуємо READY від сервера
            response = self.process_response(self.socket.recv(1024).decode().strip())
            if response != "READY":
                raise Exception(f"Неочікувана відповідь сервера: {response}")

            # Модифікуємо ім'я файлу, додаючи data_dir_num
            base_filename = os.path.basename(model_path)
            filename = f"client_{self.data_dir_num}_{base_filename}"
            self.socket.sendall(f"{filename}\n".encode())

            response = self.process_response(self.socket.recv(1024).decode().strip())
            if response != "OK":
                raise Exception(f"Неочікувана відповідь сервера: {response}")

            file_size = os.path.getsize(model_path)
            print("Відправляємо розмір моделі")
            self.socket.sendall(f"FILE_SIZE:{file_size}\n".encode())
            print(f"Відправляємо команду FILE_SIZE:{file_size}")


            # Очікуємо SIZE_RECEIVED
            print("Очікуємо size_received")
            response = self.process_response(self.socket.recv(1024).decode().strip())
            if response != "SIZE_RECEIVED":
                raise Exception(f"Неочікувана відповідь сервера: {response}")

            # Відправляємо файл
            with open(model_path, 'rb') as f:
                bytes_sent = 0
                while bytes_sent < file_size:
                    chunk = f.read(4096)
                    if not chunk:
                        break
                    self.socket.sendall(chunk)
                    bytes_sent += len(chunk)



            response = self.process_response(self.socket.recv(1024).decode().strip())
            if response != "OK":
                raise Exception(f"Неочікувана відповідь сервера: {response}")

            # Відправляємо кількість навчальних прикладів
            if not hasattr(self, 'last_training_samples'):
                raise Exception("Не знайдено інформацію про кількість навчальних прикладів")

            self.socket.sendall(f"DATA_COUNT:{self.last_training_samples}\n".encode())

            # Очікуємо DATA_COUNT_RECEIVED
            response = self.process_response(self.socket.recv(1024).decode().strip())
            if response != "DATA_COUNT_RECEIVED":
                raise Exception(f"Неочікувана відповідь сервера: {response}")

            print("Модель успішно відправлена на сервер")
            return True

        except Exception as e:
            print(f"Помилка відправки моделі на сервер: {e}")
            return False

    def ensure_base_model(self):
        """Перевіряє наявність базової моделі та завантажує її при необхідності"""
        if not self.base_model_loaded:
            print("Базова модель не знайдена, завантажуємо...")
            if self.download_base_model():
                self.base_model_loaded = True
                return True
            return False
        return True

    def receive_and_restore_model(self):
        """Отримання нових ваг моделі від сервера та їх відновлення"""
        try:
            # Очікуємо розмір файлу
            response = self.socket.recv(1024).decode().strip()
            if not response.isdigit():
                raise Exception(f"Неочікувана відповідь сервера: {response}")

            file_size = int(response)
            self.socket.sendall(b"OK\n")

            # Створюємо директорію для нової моделі
            new_model_path = os.path.join(self.client_dir, "received_model", f"model_{self.data_dir_num}.ckpt")

            # Завантажуємо файл
            with open(new_model_path, 'wb') as f:
                bytes_received = 0
                while bytes_received < file_size:
                    chunk = self.socket.recv(min(4096, file_size - bytes_received))
                    if not chunk:
                        raise Exception("З'єднання перервано під час завантаження моделі")
                    f.write(chunk)
                    bytes_received += len(chunk)

            print(f"Нові ваги моделі завантажено: {new_model_path}")

            # Оновлюємо шлях до базової моделі
            self.base_model_path = new_model_path
            return True

        except Exception as e:
            print(f"Помилка отримання та відновлення моделі: {e}")
            return False

    @staticmethod
    def process_response(response):
        return response.replace('\x00', '').replace('\n', '').strip()

    def run(self):
        """Основний цикл роботи клієнта"""
        if not self.connect_to_server():
            return

        # Виводимо інформацію про кількість доступних файлів та раунд повторного використання
        print(f"Доступно файлів даних: {self.total_data_files}")
        if self.data_reuse_start_round <= self.max_rounds:
            print(f"Повторне використання даних почнеться з раунду {self.data_reuse_start_round}")

        while True:
            try:
                # Перевіряємо, чи не досягнуто максимальну кількість раундів
                if self.current_round >= self.max_rounds:
                    print(f"Досягнуто максимальну кількість раундів ({self.max_rounds}). Завершення роботи.")
                    self.socket.sendall(b"MAX_ROUNDS_REACHED\n")
                    break

                # Очікуємо команду від сервера
                command = self.process_response(self.socket.recv(1024).decode().strip())
                print("Отримана команда: ", command)
                if command == "RETRAIN":
                    print(f"Отримано команду RETRAIN (раунд {self.current_round + 1}/{self.max_rounds})")

                    # Перевіряємо, чи настав раунд повторного використання даних
                    if self.current_round + 1 == self.data_reuse_start_round:
                        print(f"Початок повторного використання даних (раунд {self.current_round + 1})")

                    # Перетреновуємо модель
                    model_path = self.retrain_model()
                    if not model_path:
                        continue

                    # Відправляємо модель на сервер
                    if not self.send_model_to_server(model_path):
                        continue

                    if not self.receive_and_restore_model():
                        print("Помилка отримання та відновлення моделі")
                        continue

                    self.current_round += 1

                elif not command:
                    print("З'єднання з сервером перервано")
                    break

            except Exception as e:
                print(f"Помилка в циклі роботи клієнта: {e}")
                break

        self.socket.close()
        print("Клієнт завершив роботу")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Запуск федеративного клієнта')
    parser.add_argument('--data_dir', type=int, default=1, help='Номер директорії даних')
    parser.add_argument('--rounds', type=int, default=10, help='Максимальна кількість раундів навчання')
    parser.add_argument('--local_epochs', type=int, default=5, help='Кількість локальних епох тренування')
    args = parser.parse_args()

    client = FederatedClient(data_dir_num=args.data_dir, max_rounds=args.rounds, local_epochs=args.local_epochs)
    client.run()