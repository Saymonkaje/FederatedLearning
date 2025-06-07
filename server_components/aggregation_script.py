import os
import numpy as np
import tensorflow as tf
import pickle
import pandas as pd
import socket
import gc
import time
import matplotlib.pyplot as plt
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score
import seaborn as sns
import threading
import argparse
from util_functions import SignalPredictor, INPUT_SIZE, FEATURES, load_and_prepare_test_data
import socket
import json
import os
import struct


ALPHA = 0.1
# Додаємо парсер аргументів командного рядка
def parse_args():
    parser = argparse.ArgumentParser(description='Сервер агрегації моделей')
    parser.add_argument('--aggregation_type', type=str, choices=['sync', 'async'],
                      default='sync', help='Тип агрегації: sync (синхронне зважене середнє) або async (асинхронне)')
    parser.add_argument('--buffer_size', type=int, default=3,
                      help='Розмір буфера для завантаження моделей (кількість моделей для одночасної агрегації)')
    parser.add_argument('--alpha', type=float, default=0.1,
                      help='Значення ALPHA для асинхронної агрегації (в діапазоні (0, 1])')
    parser.add_argument('--evaluation_server_ip', type=str, default='127.0.0.1',
                      help='IP-адреса сервера оцінки')
    return parser.parse_args()


def mean_absolute_percentage_error(y_true, y_pred):
    """Обчислення середньої абсолютної процентної помилки (MAPE)"""
    mask = y_true != 0
    mape = np.mean(np.abs((y_true[mask] - y_pred[mask]) / y_true[mask])) * 100
    return mape

def load_weights(model_dir, buffer_size=1):
    """Завантаження ваг моделі з директорії з обмеженням кількості моделей"""
    weight_files = [f for f in os.listdir(model_dir) if f.endswith('.ckpt')]
    if not weight_files:
        return None, None

    # Обмежуємо кількість файлів для завантаження
    weight_files = weight_files[:buffer_size]
    print(f"Завантажуємо {len(weight_files)} моделей (розмір буфера: {buffer_size})")

    models = []
    data_counts = []

    for weight_file in weight_files:
        model = SignalPredictor()
        weight_path = os.path.join(model_dir, weight_file)

        # Завантаження ваг
        try:
            model.restore(weight_path)
            models.append(model)
            print(f"Ваги {weight_file} завантажено")
        except Exception as e:
            print(f"Помилка завантаження ваг з {weight_file}: {e}")
            # Продовжуємо обробку наступних файлів, якщо завантаження не вдалося
            continue

        # Зчитування кількості даних з відповідного файлу
        data_count_file = weight_file.replace('.ckpt', '_data_count.txt')
        data_count_path = os.path.join(model_dir, data_count_file)
        count = 0 # Значення за замовчуванням, якщо файл не знайдено або помилка
        if os.path.exists(data_count_path):
            try:
                with open(data_count_path, 'r') as f:
                    count_str = f.read().strip()
                    if count_str:
                        count = int(count_str)
                print(f"Кількість даних {count} зчитано з {data_count_file}")
                # Видалення файлу кількості даних після зчитування
                os.remove(data_count_path)
                print(f"Файл {data_count_file} видалено")
            except Exception as e:
                print(f"Помилка зчитування або видалення файлу {data_count_file}: {e}")
        else:
            print(f"Файл кількості даних {data_count_file} не знайдено.")

        data_counts.append(count)

        # Видалення файлів ваг після завантаження (зроблю це після обробки data_count_file)
        try:
            os.remove(weight_path)
            print(f"Ваги {weight_file} видалено")
        except Exception as e:
            print(f"Помилка видалення файлу {weight_file}: {e}")

    # Перевіряємо, чи кількість моделей і кількість даних збігаються
    if len(models) != len(data_counts):
        print("Попередження: Кількість завантажених моделей і кількість зчитаних значень даних не збігається!")
        # Можливо, тут потрібна додаткова логіка для обробки цієї ситуації

    return models, data_counts

def aggregate_weights_weighted(models, data_counts):
    """Агрегація ваг моделей з використанням зваженого середнього"""
    if not models:
        return None

    total_data_count = sum(data_counts)
    data_weights = np.array(data_counts) / total_data_count

    # Створюємо нову модель для агрегованих ваг
    aggregated_model = SignalPredictor()

    # Отримуємо ваги всіх моделей
    model_weights = [model.model.get_weights() for model in models]

    # Агрегуємо ваги
    aggregated_weights = []
    for layer_idx in range(len(model_weights[0])):
        weighted_sum = sum(model_weights[j][layer_idx] * data_weights[j]
                         for j in range(len(models)))
        aggregated_weights.append(weighted_sum)

    # Встановлюємо агреговані ваги
    aggregated_model.model.set_weights(aggregated_weights)

    return aggregated_model

def aggregate_weights_async(models, global_model, alpha=None):
    """Агрегація ваг моделей з використанням асинхронного навчання"""
    if not models:
        return None

    # Використовуємо значення ALPHA з аргументів, якщо воно передано
    if alpha is None:
        alpha = ALPHA

    # Створюємо нову модель для агрегованих ваг
    aggregated_model = SignalPredictor()

    # Отримуємо ваги глобальної моделі
    global_weights = global_model.model.get_weights()

    # Отримуємо ваги локальних моделей
    local_weights = [model.model.get_weights() for model in models]

    # Агрегуємо ваги за формулою: w_global(t+1) = (1-α)w_global(t) + αw_k
    aggregated_weights = []
    for layer_idx in range(len(global_weights)):
        # Беремо середнє значення локальних моделей
        local_avg = np.mean([weights[layer_idx] for weights in local_weights], axis=0)
        # Застосовуємо формулу асинхронної агрегації
        weighted_sum = (1 - alpha) * global_weights[layer_idx] + alpha * local_avg
        aggregated_weights.append(weighted_sum)

    # Встановлюємо агреговані ваги
    aggregated_model.model.set_weights(aggregated_weights)

    return aggregated_model

def save_model(model, model_dir):
    """Збереження моделі"""
    if model:
        os.makedirs(model_dir, exist_ok=True)
        existing_models = [f for f in os.listdir(model_dir) if f.startswith("global_model_") and f.endswith(".ckpt")]
        new_index = len(existing_models) + 1
        save_path = os.path.join(model_dir, f"global_model_{new_index}.ckpt")
        model.save(save_path)
        print(f"Агрегована модель збережена в {save_path}")
        return save_path


def trigger_remote_evaluation(model_path, host=None, port=54321):
    """Надсилає модель на сервер оцінки і чекає тільки підтвердження отримання."""
    if host is None:
        host = '127.0.0.1'  # Використовуємо localhost якщо IP не вказано

    try:
        print(f"Надсилаємо модель {model_path} на сервер оцінки {host}:{port}...")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(30)  # Повертаємо таймаут до 30 секунд, оскільки тепер чекаємо тільки отримання файлу
            s.connect((host, port))

            # 1. Надіслати назву файлу
            model_filename = os.path.basename(model_path)
            s.sendall(model_filename.encode())

            # Очікуємо підтвердження отримання назви файлу
            response = s.recv(1024).decode().strip()

            print("received response after filename")
            if response != "FILENAME_RECEIVED":
                print(f"Неочікувана відповідь сервера при відправці назви файлу: {response}")
                return

            # Надсилаємо розмір файлу
            file_size = os.path.getsize(model_path)
            s.sendall(struct.pack('>Q', file_size))

            # Надсилаємо файл частинами
            with open(model_path, 'rb') as f:
                while True:
                    bytes_read = f.read(4096)
                    if not bytes_read:
                        break
                    s.sendall(bytes_read)


            # Очікуємо підтвердження отримання файлу
            response = s.recv(1024).decode().strip()
            print("received response after file")

            if response == "EVALUATION_STARTED":
                print("Файл успішно отримано сервером і почато оцінку")
            elif response.startswith("ERROR_"):
                print(f"Помилка на сервері: {response}")
            else:
                print(f"Неочікувана відповідь сервера при відправці файлу: {response}")

    except socket.timeout:
        print("Таймаут з'єднання з сервером оцінки")
    except ConnectionRefusedError:
        print("Не вдалося підключитися до сервера оцінки")
    except Exception as e:
        print(f"Помилка при відправці моделі на сервер оцінки: {e}")


global_model = SignalPredictor()


def handle_client_connection(client_socket, args):
    """Обробка підключення клієнта"""
    MODEL_DIR = "./aggregation_models"
    GLOBAL_MODEL_DIR = "./global_model"
    BASE_MODEL_PATH = "./base_model/big_global_model_weights.ckpt"

    global_model_loaded = False

    # Спочатку перевіряємо наявність агрегованих моделей
    existing_models = [f for f in os.listdir(GLOBAL_MODEL_DIR) if f.startswith("global_model_") and f.endswith(".ckpt")]

    if existing_models:
        # Сортуємо моделі за номером, використовуючи правильне числове сортування
        def get_model_number(filename):
            try:
                # Видаляємо 'global_model_' з початку та '.ckpt' з кінця, щоб отримати номер
                number = int(filename.replace('global_model_', '').replace('.ckpt', ''))
                return number
            except ValueError:
                return -1  # Якщо не вдалося отримати номер, повертаємо -1

        # Сортуємо за номером моделі
        latest_model = max(existing_models, key=get_model_number)
        latest_model_path = os.path.join(GLOBAL_MODEL_DIR, latest_model)
        try:
            global_model.restore(latest_model_path)
            print(f"Ваги останньої агрегованої моделі успішно завантажено з {latest_model}")
            global_model_loaded = True
        except Exception as e:
            print(f"Помилка завантаження останньої агрегованої моделі: {e}")

    # Якщо не вдалося завантажити агреговану модель, спробуємо завантажити базову модель
    if not global_model_loaded and os.path.exists(BASE_MODEL_PATH):
        try:
            global_model.restore(BASE_MODEL_PATH)
            print("Ваги базової моделі успішно завантажено")
            global_model_loaded = True
        except Exception as e:
            print(f"Помилка завантаження базової моделі: {e}")

    if not global_model_loaded:
        print("Не вдалося завантажити жодну модель. Створюємо нову модель з випадковими вагами")

    # Завантажуємо моделі для агрегації з урахуванням розміру буфера
    models, data_counts = load_weights(MODEL_DIR, args.buffer_size)
    if models:
        if args.aggregation_type == 'sync':
            aggregated_model = aggregate_weights_weighted(models, data_counts)
        else:  # async
            aggregated_model = aggregate_weights_async(models, global_model, alpha=args.alpha)

        if aggregated_model:
            saved_model_path = save_model(aggregated_model, GLOBAL_MODEL_DIR)

            # Запускаємо оцінку в окремому потоці
            if saved_model_path:
                trigger_remote_evaluation(saved_model_path, host=args.evaluation_server_ip)

            # Надсилаємо повідомлення клієнту
            model_filename = os.path.basename(saved_model_path)

            completion_message = f"Script execution completed. Filename: {model_filename}"
            client_socket.send(completion_message.encode())
    else:
        completion_message = "No models to aggregate"
        client_socket.send(completion_message.encode())

    client_socket.close()

def start_server():
    """Запуск сервера"""
    args = parse_args()
    print(f"Запуск сервера з типом агрегації: {args.aggregation_type}")

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind(('0.0.0.0', 12345))
    server_socket.listen(5)
    print("Сервер запущено. Очікування підключень...")

    while True:
        client_socket, addr = server_socket.accept()
        print(f"Підключено клієнта: {addr}")
        handle_client_connection(client_socket, args)

if __name__ == "__main__":
    # Створюємо директорії для результатів тестування та глобальної моделі
    os.makedirs('testing_result', exist_ok=True)
    os.makedirs('global_model', exist_ok=True)
    start_server()