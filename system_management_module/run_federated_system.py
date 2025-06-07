import subprocess
import sys
import os
import time
import argparse
import signal
import psutil
import threading
import queue
import tkinter as tk
from tkinter import ttk, scrolledtext
from datetime import datetime
import socket
import json
import glob
import re

def find_evaluation_server(broadcast_port=49152, timeout=5):
    """
    Пошук сервера оцінки в локальній мережі через broadcast.
    Повертає IP знайденого сервера або '127.0.0.1' якщо сервер не знайдено.
    """
    try:
        # Створюємо UDP сокет для broadcast
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)

        # Отримуємо локальну IP-адресу та маску підмережі
        hostname = socket.gethostname()
        local_ip = socket.gethostbyname(hostname)

        # Створюємо broadcast адресу для локальної мережі
        ip_parts = local_ip.split('.')
        broadcast_ip = f"{ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.255"
        print(f"Використовуємо broadcast адресу: {broadcast_ip}")

        # Відправляємо broadcast запит
        broadcast_message = b"EVALUATION_SERVER_DISCOVERY"
        sock.sendto(broadcast_message, (broadcast_ip, broadcast_port))
        print(f"Відправлено broadcast запит на {broadcast_ip}:{broadcast_port}")

        # Чекаємо відповідь
        try:
            data, addr = sock.recvfrom(1024)
            if data == b"EVALUATION_SERVER_RESPONSE":
                print(f"Знайдено сервер оцінки на {addr[0]}")
                return addr[0]
        except socket.timeout:
            print("Таймаут пошуку сервера оцінки, використовуємо localhost")
            return '127.0.0.1'
    except Exception as e:
        print(f"Помилка при пошуку сервера оцінки: {e}")
        return '127.0.0.1'
    finally:
        sock.close()

class FederatedSystemGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Федеративна система")
        self.root.geometry("1200x800")  # Збільшуємо розмір вікна

        # Додаємо обробник закриття вікна
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # Змінні
        self.processes = {}  # Змінюємо на словник для зберігання процесів з їх типами
        self.output_queue = queue.Queue()
        self.is_running = False
        self.client_states = {}  # Словник для зберігання станів клієнтів
        self.client_logs = {}    # Словник для зберігання логів клієнтів
        self.server_log = None  # Додаємо змінну для логів сервера
        self.aggregation_log = None  # Додаємо змінну для логів агрегації
        self.aggregation_mode = tk.StringVar(value="async")  # Змінна для зберігання режиму агрегації
        self.buffer_size = tk.StringVar(value="1")  # Змінна для зберігання розміру буфера
        self.alpha_value = tk.StringVar(value="0.1")  # Змінна для зберігання значення ALPHA
        self.rounds_count = tk.StringVar(value="10")  # Змінна для зберігання кількості раундів
        self.local_epochs = tk.StringVar(value="5")  # Змінна для зберігання кількості локальних епох
        self.evaluation_server_ip = None  # Змінна для зберігання IP сервера оцінки
        self.eval_server_status = tk.StringVar(value="Статус сервера оцінки: Перевірка...")  # Ініціалізуємо змінну статусу
        self.metrics_socket = None  # Ініціалізуємо сокет як None
        self.metrics_socket_lock = threading.Lock()  # Додаємо блокування для сокета
        self.data_reuse_info = None  # Змінна для зберігання інформації про data reuse

        # Створення GUI елементів
        self.create_widgets()

        self.on_aggregation_mode_change()

        # Додаємо обробник зміни режиму агрегації
        self.aggregation_mode.trace_add("write", self.on_aggregation_mode_change)

        # Запуск потоку для оновлення метрик
        self.update_thread = threading.Thread(target=self.update_metrics, daemon=True)
        self.update_thread.start()

        # Оновлюємо статус сервера після створення всіх віджетів
        self.update_evaluation_server_status()

        # Ініціалізуємо сокет для метрик
        self.initialize_metrics_socket()

        # Запуск потоку для отримання метрик
        self.metrics_thread = threading.Thread(target=self.receive_metrics, daemon=True)
        self.metrics_thread.start()

    def create_widgets(self):
        # Верхня панель з керуванням
        control_frame = ttk.Frame(self.root, padding="5")
        control_frame.pack(fill=tk.X)

        # Створюємо фрейм для статусу сервера та кнопок
        server_status_frame = ttk.Frame(self.root, padding="5")
        server_status_frame.pack(fill=tk.X)

        ttk.Label(control_frame, text="Кількість клієнтів:").pack(side=tk.LEFT, padx=5)
        self.clients_var = tk.StringVar(value="5")
        self.clients_entry = ttk.Entry(control_frame, textvariable=self.clients_var, width=10)
        self.clients_entry.pack(side=tk.LEFT, padx=5)

        ttk.Label(control_frame, text="Кількість раундів:").pack(side=tk.LEFT, padx=5)
        self.rounds_entry = ttk.Entry(control_frame, textvariable=self.rounds_count, width=10)
        self.rounds_entry.pack(side=tk.LEFT, padx=5)

        ttk.Label(control_frame, text="Локальні епохи:").pack(side=tk.LEFT, padx=5)
        self.epochs_entry = ttk.Entry(control_frame, textvariable=self.local_epochs, width=10)
        self.epochs_entry.pack(side=tk.LEFT, padx=5)

        # Додаємо радіокнопки для вибору режиму агрегації
        ttk.Label(control_frame, text="Режим агрегації:").pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(control_frame, text="Асинхронний", variable=self.aggregation_mode,
                       value="async", command=lambda: self.on_aggregation_mode_change()).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(control_frame, text="Синхронний", variable=self.aggregation_mode,
                       value="sync", command=lambda: self.on_aggregation_mode_change()).pack(side=tk.LEFT, padx=5)

        ttk.Label(control_frame, text="Розмір буфера:").pack(side=tk.LEFT, padx=5)
        self.buffer_entry = ttk.Entry(control_frame, textvariable=self.buffer_size, width=10)
        self.buffer_entry.pack(side=tk.LEFT, padx=5)

        ttk.Label(control_frame, text="ALPHA:").pack(side=tk.LEFT, padx=5)
        self.alpha_entry = ttk.Entry(control_frame, textvariable=self.alpha_value, width=10)
        self.alpha_entry.pack(side=tk.LEFT, padx=5)

        # Додаємо роздільник
        ttk.Separator(control_frame, orient='vertical').pack(side=tk.LEFT, padx=10, fill=tk.Y)

        # Додаємо статус сервера оцінки до нового фрейму
        self.eval_server_label = ttk.Label(server_status_frame, textvariable=self.eval_server_status)
        self.eval_server_label.pack(side=tk.LEFT, padx=5)

        # Додаємо кнопку для повторного пошуку сервера до нового фрейму
        self.rescan_button = ttk.Button(server_status_frame, text="Перешукати сервер оцінки", command=self.rescan_evaluation_server)
        self.rescan_button.pack(side=tk.LEFT, padx=5)

        # Кнопка для перегляду логів сервера до нового фрейму
        self.view_server_logs_button = ttk.Button(server_status_frame, text="Логи сервера координатора", command=self.show_server_logs)
        self.view_server_logs_button.pack(side=tk.LEFT, padx=5)

        # Додаємо кнопку для перегляду логів агрегації до нового фрейму
        self.view_aggregation_logs_button = ttk.Button(server_status_frame, text="Логи агрегації", command=self.show_aggregation_logs)
        self.view_aggregation_logs_button.pack(side=tk.LEFT, padx=5)

        # Додаємо кнопки керування в server_status_frame
        self.start_button = ttk.Button(server_status_frame, text="Запустити", command=self.start_system)
        self.start_button.pack(side=tk.LEFT, padx=5)

        self.stop_button = ttk.Button(server_status_frame, text="Зупинити", command=self.stop_system, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=5)

        # Створюємо фрейм для розміщення всіх блоків
        content_frame = ttk.Frame(self.root)
        content_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Ліва частина (метрики та стани)
        left_frame = ttk.Frame(content_frame)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        # Вікно для метрик
        metrics_frame = ttk.LabelFrame(left_frame, text="Метрики глобальної моделі", padding="5")
        metrics_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        self.metrics_text = scrolledtext.ScrolledText(metrics_frame, wrap=tk.WORD, height=15)
        self.metrics_text.pack(fill=tk.BOTH, expand=True)
        self.metrics_text.config(state=tk.DISABLED)

        # Вікно для станів клієнтів
        clients_frame = ttk.LabelFrame(left_frame, text="Стани клієнтів", padding="5")
        clients_frame.pack(fill=tk.BOTH, expand=True)

        self.clients_text = scrolledtext.ScrolledText(clients_frame, wrap=tk.WORD, height=10)
        self.clients_text.pack(fill=tk.BOTH, expand=True)
        self.clients_text.config(state=tk.DISABLED)

        # Права частина (логи клієнтів)
        logs_frame = ttk.LabelFrame(content_frame, text="Логи клієнтів", padding="5")
        logs_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))

        # Створюємо нотебук для вкладок з логами клієнтів
        self.logs_notebook = ttk.Notebook(logs_frame)
        self.logs_notebook.pack(fill=tk.BOTH, expand=True)

    def create_client_log_tab(self, client_id):
        """Створення нової вкладки для логів клієнта"""
        if client_id not in self.client_logs:
            frame = ttk.Frame(self.logs_notebook)
            text = scrolledtext.ScrolledText(frame, wrap=tk.WORD)
            text.pack(fill=tk.BOTH, expand=True)
            text.config(state=tk.DISABLED)
            self.client_logs[client_id] = text
            self.logs_notebook.add(frame, text=f"Клієнт {client_id}")

    def update_client_log(self, client_id, message):
        """Оновлення логів клієнта"""
        if client_id not in self.client_logs:
            self.create_client_log_tab(client_id)

        text = self.client_logs[client_id]
        text.config(state=tk.NORMAL)
        timestamp = datetime.now().strftime("%H:%M:%S")
        text.insert(tk.END, f"[{timestamp}] {message}\n")
        text.see(tk.END)
        text.config(state=tk.DISABLED)

    def update_client_state(self, client_id, message):
        """Оновлення стану клієнта (виправлена версія)"""
        if client_id is None:
            return

        # Крок 1: Переконатися, що запис для клієнта існує. Якщо ні - створити.
        # Це централізує ініціалізацію і виконується лише один раз для кожного клієнта.
        if client_id not in self.client_states:
            self.client_states[client_id] = {"state": "🚀 Запускається", "round": 0}

        # Крок 2: Перевірити, чи повідомлення містить інформацію про раунд.
        round_match = re.search(r'Отримано команду RETRAIN \(раунд (\d+)/(\d+)\)', message)
        if round_match:
            print("found round = ", round_match)
            current_round = int(round_match.group(1))
            self.client_states[client_id]["round"] = current_round

        if "Підключено до сервера" in message:
            self.client_states[client_id]["state"] = "✅ Підключено"
        elif "Початок перетренування моделі" in message:
            self.client_states[client_id]["state"] = "🔄 Тренування"
        elif "Модель перетренована" in message:
            self.client_states[client_id]["state"] = "📤 Очікування відправки моделі"
        elif "Нові ваги моделі завантажено" in message:
            self.client_states[client_id]["state"] = "📥 Отримання моделі"
        elif "Модель успішно відправлена на сервер" in message:
            self.client_states[client_id]["state"] = "⏳ Очікування"
        elif "Клієнт завершив роботу" in message or "З'єднання з сервером перервано" in message:
            self.client_states[client_id]["state"] = "❌ Відключено"

        # Крок 4: Перемалювати віджет зі станами клієнтів.
        self.clients_text.config(state=tk.NORMAL)
        self.clients_text.delete(1.0, tk.END)
        for cid in sorted(self.client_states.keys()):
            client_info = self.client_states[cid]
            state = client_info["state"]
            round_num = client_info["round"]
            self.clients_text.insert(tk.END, f"Клієнт {cid}: {state} (Раунд: {round_num})\n")
        self.clients_text.see(tk.END)
        self.clients_text.config(state=tk.DISABLED)

    def show_server_logs(self):
        """Показ логів сервера в окремому вікні"""
        if not hasattr(self, 'server_log_window') or not self.server_log_window.winfo_exists():
            self.server_log_window = tk.Toplevel(self.root)
            self.server_log_window.title("Логи сервера")
            self.server_log_window.geometry("800x600")

            # Створюємо текстове поле для логів
            server_log_text = scrolledtext.ScrolledText(self.server_log_window, wrap=tk.WORD)
            server_log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
            server_log_text.config(state=tk.DISABLED)

            # Копіюємо логи сервера
            if self.server_log:
                server_log_text.config(state=tk.NORMAL)
                server_log_text.delete(1.0, tk.END)
                server_log_text.insert(tk.END, self.server_log)
                server_log_text.see(tk.END)
                server_log_text.config(state=tk.DISABLED)

            # Додаємо кнопку оновлення
            def update_logs():
                if self.server_log:
                    server_log_text.config(state=tk.NORMAL)
                    server_log_text.delete(1.0, tk.END)
                    server_log_text.insert(tk.END, self.server_log)
                    server_log_text.see(tk.END)
                    server_log_text.config(state=tk.DISABLED)

            update_button = ttk.Button(self.server_log_window, text="Оновити", command=update_logs)
            update_button.pack(pady=5)

    def show_aggregation_logs(self):
        """Показ логів агрегаційного скрипту в окремому вікні"""
        if not hasattr(self, 'aggregation_log_window') or not self.aggregation_log_window.winfo_exists():
            self.aggregation_log_window = tk.Toplevel(self.root)
            self.aggregation_log_window.title("Логи агрегаційного скрипту")
            self.aggregation_log_window.geometry("800x600")

            # Створюємо текстове поле для логів
            aggregation_log_text = scrolledtext.ScrolledText(self.aggregation_log_window, wrap=tk.WORD)
            aggregation_log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
            aggregation_log_text.config(state=tk.DISABLED)

            # Копіюємо логи агрегації
            if self.aggregation_log:
                aggregation_log_text.config(state=tk.NORMAL)
                aggregation_log_text.delete(1.0, tk.END)
                aggregation_log_text.insert(tk.END, self.aggregation_log)
                aggregation_log_text.see(tk.END)
                aggregation_log_text.config(state=tk.DISABLED)

            # Додаємо кнопку оновлення
            def update_logs():
                if self.aggregation_log:
                    aggregation_log_text.config(state=tk.NORMAL)
                    aggregation_log_text.delete(1.0, tk.END)
                    aggregation_log_text.insert(tk.END, self.aggregation_log)
                    aggregation_log_text.see(tk.END)
                    aggregation_log_text.config(state=tk.DISABLED)

            update_button = ttk.Button(self.aggregation_log_window, text="Оновити", command=update_logs)
            update_button.pack(pady=5)

    def update_metrics(self):
        """Оновлення метрик у вікні"""
        client_keywords = [
            'Підключено до сервера',
            'Початок перетренування моделі',
            'Модель перетренована',
            'Відправляємо розмір моделі',
            'RETRAIN',
            'Нові ваги моделі завантажено',
            'Модель успішно відправлена на сервер',
            'Клієнт завершив роботу',
            'З\'єднання з сервером перервано'
        ]

        while True:
            try:
                process_type, client_id, line = self.output_queue.get(timeout=0.1)

                # Обробляємо логи в залежності від типу процесу
                if process_type == 'client' and client_id is not None:
                    # Оновлюємо логи клієнта
                    self.update_client_log(client_id, line)

                    # Перевіряємо стан клієнта
                    for keyword in client_keywords:
                        if keyword in line:
                            self.update_client_state(client_id, line)
                            break

                elif process_type == 'server':
                    # Зберігаємо логи сервера
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    log_entry = f"[{timestamp}] {line}\n"

                    if self.server_log is None:
                        self.server_log = log_entry
                    else:
                        self.server_log += log_entry

                elif process_type == 'aggregation':
                    # Зберігаємо логи агрегації
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    log_entry = f"[{timestamp}] {line}\n"

                    if self.aggregation_log is None:
                        self.aggregation_log = log_entry
                    else:
                        self.aggregation_log += log_entry

            except queue.Empty:
                continue
            except Exception as e:
                print(f"Помилка при оновленні метрик: {e}")

    def read_output(self, process, process_type, client_id=None):
        """Читання виводу процесу"""
        for line in iter(process.stdout.readline, ''):
            try:
                decoded_line = line.encode('utf-8').decode('utf-8').strip()
                # Додаємо інформацію про тип процесу та ID клієнта
                self.output_queue.put((process_type, client_id, decoded_line))
            except UnicodeError:
                try:
                    decoded_line = line.encode('cp1251').decode('cp1251').strip()
                    self.output_queue.put((process_type, client_id, decoded_line))
                except UnicodeError:
                    print(f"Помилка кодування рядка: {line}")
        process.stdout.close()

    def check_data_reuse_info(self):
        """Перевірка інформації про data reuse для всіх клієнтів"""
        try:
            num_clients = int(self.clients_var.get())
            if num_clients < 1:
                return

            reuse_info = []
            for i in range(1, num_clients + 1):
                client_dir = f"federated_clients/client{i}/data"
                data_files = sorted(glob.glob(os.path.join(client_dir, "data*.txt")))
                total_files = len(data_files)
                if total_files == 0:
                    reuse_info.append(f"Клієнт {i}: Помилка - не знайдено файлів даних")
                    continue

                rounds = int(self.rounds_count.get())
                if rounds > total_files:
                    reuse_info.append(f"Клієнт {i}: {total_files} файлів даних, перевикористання даних з раунду {total_files + 1}")
                else:
                    reuse_info.append(f"Клієнт {i}: {total_files} файлів даних, перевикористання даних не потрібне")

            self.data_reuse_info = "\n".join(reuse_info)
            return True
        except Exception as e:
            print(f"Помилка при перевірці data reuse: {e}")
            return False

    def start_system(self):
        """Запуск системи"""
        try:
            num_clients = int(self.clients_var.get())
            if num_clients < 1:
                raise ValueError("Кількість клієнтів повинна бути більше 0")
        except ValueError as e:
            self.metrics_text.config(state=tk.NORMAL)
            self.metrics_text.insert(tk.END, f"\nПомилка: {str(e)}\n")
            self.metrics_text.config(state=tk.DISABLED)
            return

        # Перевіряємо інформацію про data reuse
        if not self.check_data_reuse_info():
            self.metrics_text.config(state=tk.NORMAL)
            self.metrics_text.insert(tk.END, "\nПомилка при перевірці data reuse\n")
            self.metrics_text.config(state=tk.DISABLED)
            return

        # Виводимо інформацію про data reuse
        self.metrics_text.config(state=tk.NORMAL)
        self.metrics_text.insert(tk.END, "\nІнформація про data reuse:\n")
        self.metrics_text.insert(tk.END, self.data_reuse_info + "\n\n")
        self.metrics_text.config(state=tk.DISABLED)

        self.is_running = True
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.clients_entry.config(state=tk.DISABLED)

        # Переініціалізуємо сокет для метрик
        self.initialize_metrics_socket()

        # Очищаємо стани клієнтів
        self.client_states.clear()
        self.client_logs.clear()  # Очищаємо логи клієнтів
        self.clients_text.config(state=tk.NORMAL)
        self.clients_text.delete(1.0, tk.END)
        self.clients_text.config(state=tk.DISABLED)

        # Запуск системи в окремому потоці
        threading.Thread(target=self.run_system, args=(num_clients,), daemon=True).start()

    def on_closing(self):
        """Обробник закриття вікна"""
        if self.is_running:
            self.stop_system()
        self.root.destroy()

    def stop_system(self):
        """Зупинка системи"""
        self.is_running = False

        # Закриваємо сокет для метрик
        with self.metrics_socket_lock:
            if self.metrics_socket is not None:
                print("Закриваємо старий сокет для метрик...")
                try:
                    self.metrics_socket.shutdown(socket.SHUT_RDWR)
                except:
                    pass
                try:
                    self.metrics_socket.close()
                except:
                    pass
                self.metrics_socket = None
                print("Старий сокет для метрик закрито")

        # Зупиняємо всі процеси
        for process_name, process in list(self.processes.items()):
            try:
                if process.poll() is None:  # Перевіряємо чи процес все ще працює
                    kill_process_tree(process.pid)
                    process.wait(timeout=5)  # Чекаємо завершення процесу
            except Exception as e:
                print(f"Помилка при зупинці процесу {process_name}: {e}")
            finally:
                try:
                    process.kill()  # Примусово завершуємо процес якщо він все ще працює
                except:
                    pass

        self.processes.clear()
        self.client_states.clear()
        self.client_logs.clear()
        self.server_log = None
        self.aggregation_log = None  # Очищаємо логи агрегації

        # Очищаємо всі вкладки
        for tab in self.logs_notebook.tabs():
            self.logs_notebook.forget(tab)

        self.clients_text.config(state=tk.NORMAL)
        self.clients_text.delete(1.0, tk.END)
        self.clients_text.config(state=tk.DISABLED)

        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.clients_entry.config(state=tk.NORMAL)

        self.metrics_text.config(state=tk.NORMAL)
        self.metrics_text.insert(tk.END, "\nСистема зупинена\n")
        self.metrics_text.config(state=tk.DISABLED)

    def run_system(self, num_clients):
        """Запуск всіх компонентів системи"""
        try:
            # Перевіряємо розмір буфера
            try:
                buffer_size = int(self.buffer_size.get())
                if buffer_size < 1:
                    raise ValueError("Розмір буфера повинен бути більше 0")
            except ValueError as e:
                self.metrics_text.config(state=tk.NORMAL)
                self.metrics_text.insert(tk.END, f"\nПомилка: {str(e)}\n")
                self.metrics_text.config(state=tk.DISABLED)
                return

            # Перевіряємо значення ALPHA
            try:
                alpha = float(self.alpha_value.get())
                if not 0 < alpha <= 1:
                    raise ValueError("ALPHA повинен бути в діапазоні (0, 1]")
            except ValueError as e:
                self.metrics_text.config(state=tk.NORMAL)
                self.metrics_text.insert(tk.END, f"\nПомилка: {str(e)}\n")
                self.metrics_text.config(state=tk.DISABLED)
                return

            # Перевіряємо кількість раундів
            try:
                rounds = int(self.rounds_count.get())
                if rounds < 1:
                    raise ValueError("Кількість раундів повинна бути більше 0")
            except ValueError as e:
                self.metrics_text.config(state=tk.NORMAL)
                self.metrics_text.insert(tk.END, f"\nПомилка: {str(e)}\n")
                self.metrics_text.config(state=tk.DISABLED)
                return

            # Перевіряємо кількість локальних епох
            try:
                local_epochs = int(self.local_epochs.get())
                if local_epochs < 1:
                    raise ValueError("Кількість локальних епох повинна бути більше 0")
            except ValueError as e:
                self.metrics_text.config(state=tk.NORMAL)
                self.metrics_text.insert(tk.END, f"\nПомилка: {str(e)}\n")
                self.metrics_text.config(state=tk.DISABLED)
                return

            # Запуск C++ сервера
            self.metrics_text.config(state=tk.NORMAL)
            self.metrics_text.insert(tk.END, "\nЗапуск C++ сервера...\n")
            self.metrics_text.config(state=tk.DISABLED)

            server_process = subprocess.Popen(
                ["./aggregation_server_bchr/x64/Release/aggregation_server_bchr.exe", str(buffer_size)],
                cwd="aggregation_server_bchr",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace'
            )
            self.processes['server'] = server_process
            threading.Thread(target=self.read_output, args=(server_process, 'server'), daemon=True).start()

            time.sleep(2)

            # Запуск скрипту агрегації
            self.metrics_text.config(state=tk.NORMAL)
            self.metrics_text.insert(tk.END, "Запуск скрипту агрегації...\n")
            self.metrics_text.config(state=tk.DISABLED)

            aggregation_process = subprocess.Popen(
                [sys.executable, "aggregation_module.py",
                 "--aggregation_type", self.aggregation_mode.get(),
                 "--buffer_size", str(buffer_size),
                 "--alpha", str(alpha),
                 "--evaluation_server_ip", self.evaluation_server_ip],  # Додаємо IP сервера оцінки
                cwd="aggregation_server_bchr",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
                universal_newlines=True
            )
            self.processes['aggregation'] = aggregation_process
            threading.Thread(target=self.read_output, args=(aggregation_process, 'aggregation'), daemon=True).start()

            time.sleep(2)

            # Запуск клієнтів
            self.metrics_text.config(state=tk.NORMAL)
            self.metrics_text.insert(tk.END, f"Запуск {num_clients} клієнтів...\n")
            self.metrics_text.config(state=tk.DISABLED)

            for i in range(1, num_clients + 1):
                if not self.is_running:
                    break

                # Створюємо вкладку для логів клієнта
                self.create_client_log_tab(i)
                self.client_states[i] = {"state": "⏳ Запуск", "round": 0}
                self.update_client_state(i, "")

                client_process = subprocess.Popen(
                    [sys.executable, "federated_client.py",
                     "--data_dir", str(i),
                     "--rounds", str(rounds),
                     "--local_epochs", str(local_epochs)],
                    cwd="federated_clients",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding='utf-8',
                    errors='replace'
                )
                self.processes[f'client_{i}'] = client_process
                threading.Thread(target=self.read_output, args=(client_process, 'client', i), daemon=True).start()
                time.sleep(1)
            print("started all cients")

            # Очікування завершення роботи
            while self.is_running:
                # Перевіряємо тільки критичні процеси (сервер та агрегацію)
                critical_processes = {k: v for k, v in self.processes.items() if k in ['server', 'aggregation']}
                if not all(process.poll() is None for process in critical_processes.values()):
                    print("Критичний процес завершив роботу, зупиняємо систему")
                    break

                # Перевіряємо клієнтів окремо
                client_processes = {k: v for k, v in self.processes.items() if k.startswith('client_')}
                for client_id, process in list(client_processes.items()):
                    if process.poll() is not None:
                        print(f"Клієнт {client_id} завершив роботу")
                        del self.processes[client_id]
                        # Оновлюємо стан клієнта
                        client_num = int(client_id.split('_')[1])
                        self.update_client_state(client_num, "Клієнт завершив роботу")

                time.sleep(1)

        except Exception as e:
            self.metrics_text.config(state=tk.NORMAL)
            self.metrics_text.insert(tk.END, f"\nПомилка: {str(e)}\n")
            self.metrics_text.config(state=tk.DISABLED)
        finally:
            if self.is_running:
                self.stop_system()

    def on_aggregation_mode_change(self, *args):
        """Обробник зміни режиму агрегації"""
        if self.aggregation_mode.get() == "async":
            # При асинхронній агрегації встановлюємо розмір буфера в 1
            self.buffer_size.set("1")
            self.buffer_entry.config(state=tk.DISABLED)
            self.alpha_entry.config(state=tk.NORMAL)  # Розблоковуємо поле ALPHA
        else:
            # При синхронній агрегації розблоковуємо поле введення буфера
            self.buffer_entry.config(state=tk.NORMAL)
            self.alpha_entry.config(state=tk.DISABLED)  # Блокуємо поле ALPHA

    def initialize_metrics_socket(self):
        """Ініціалізація сокета для отримання метрик"""
        with self.metrics_socket_lock:
            try:
                # Закриваємо старий сокет, якщо він існує
                if self.metrics_socket is not None:
                    print("Закриваємо старий сокет для метрик...")
                    try:
                        self.metrics_socket.shutdown(socket.SHUT_RDWR)
                    except:
                        pass
                    try:
                        self.metrics_socket.close()
                    except:
                        pass
                    self.metrics_socket = None
                    print("Старий сокет для метрик закрито")

                print("Створюємо новий сокет для метрик...")
                # Створюємо новий сокет
                self.metrics_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.metrics_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.metrics_socket.settimeout(1.0)  # Встановлюємо таймаут
                self.metrics_socket.bind(('0.0.0.0', 54322))  # Слухаємо на всіх інтерфейсах
                self.metrics_socket.listen(1)
                print("Сокет для отримання метрик успішно ініціалізовано на порту 54322")
            except Exception as e:
                print(f"Помилка ініціалізації сокету для метрик: {e}")
                if self.metrics_socket is not None:
                    try:
                        self.metrics_socket.close()
                    except:
                        pass
                self.metrics_socket = None
                print("Спроба переініціалізації сокету через 5 секунд...")
                time.sleep(5)
                self.initialize_metrics_socket()  # Рекурсивно пробуємо переініціалізувати

    def receive_metrics(self):
        """Отримання метрик від сервера оцінки"""
        while True:
            try:
                with self.metrics_socket_lock:
                    if self.metrics_socket is None:
                        time.sleep(1)
                        continue

                    try:
                        client_socket, addr = self.metrics_socket.accept()
                        print(f"Отримано з'єднання для метрик від {addr}")

                        try:
                            data = client_socket.recv(4096).decode()
                            if data:
                                try:
                                    metrics = json.loads(data)
                                    timestamp = datetime.now().strftime("%H:%M:%S")
                                    model_name = metrics.pop('model_name', 'Невідома модель')
                                    self.metrics_text.config(state=tk.NORMAL)
                                    self.metrics_text.insert(tk.END, f"\n[{timestamp}] Метрики для моделі: {model_name}\n")
                                    for metric_name, value in metrics.items():
                                        if metric_name == 'MAPE':
                                            self.metrics_text.insert(tk.END, f"{metric_name}: {value:.2f}%\n")
                                        else:
                                            self.metrics_text.insert(tk.END, f"{metric_name}: {value:.4f}\n")
                                    self.metrics_text.see(tk.END)
                                    self.metrics_text.config(state=tk.DISABLED)
                                except json.JSONDecodeError as e:
                                    print(f"Помилка декодування JSON метрик: {e}")
                        finally:
                            try:
                                client_socket.shutdown(socket.SHUT_RDWR)
                            except:
                                pass
                            try:
                                client_socket.close()
                            except:
                                pass

                    except socket.timeout:
                        continue
                    except Exception as e:
                        print(f"Помилка при обробці з'єднання для метрик: {e}")
                        time.sleep(1)

            except Exception as e:
                print(f"Помилка отримання метрик: {e}")
                time.sleep(1)
                # Спробуємо переініціалізувати сокет
                self.initialize_metrics_socket()

    def update_evaluation_server_status(self):
        """Оновлення статусу сервера оцінки"""
        if self.evaluation_server_ip is None:
            self.evaluation_server_ip = find_evaluation_server()

        if self.evaluation_server_ip == '127.0.0.1':
            self.eval_server_status.set("Статус сервера оцінки: Локальний (localhost)")
        else:
            self.eval_server_status.set(f"Статус сервера оцінки: Віддалений ({self.evaluation_server_ip})")

    def rescan_evaluation_server(self):
        """Повторний пошук сервера оцінки та оновлення агрегаційного скрипту"""
        old_ip = self.evaluation_server_ip
        self.evaluation_server_ip = find_evaluation_server()
        self.update_evaluation_server_status()

        # Якщо IP змінився і система запущена, оновлюємо агрегаційний скрипт
        if old_ip != self.evaluation_server_ip and self.is_running and 'aggregation' in self.processes:
            try:
                # Зупиняємо поточний процес агрегації
                agg_process = self.processes['aggregation']
                if agg_process.poll() is None:
                    kill_process_tree(agg_process.pid)
                    agg_process.wait(timeout=5)

                # Перезапускаємо процес агрегації з новим IP
                buffer_size = int(self.buffer_size.get())
                alpha = float(self.alpha_value.get())

                new_agg_process = subprocess.Popen(
                    [sys.executable, "aggregation_module.py",
                     "--aggregation_type", self.aggregation_mode.get(),
                     "--buffer_size", str(buffer_size),
                     "--alpha", str(alpha),
                     "--evaluation_server_ip", self.evaluation_server_ip],
                    cwd="aggregation_server_bchr",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    bufsize=1,
                    universal_newlines=True
                )

                self.processes['aggregation'] = new_agg_process
                threading.Thread(target=self.read_output,
                               args=(new_agg_process, 'aggregation'),
                               daemon=True).start()

                self.metrics_text.config(state=tk.NORMAL)
                self.metrics_text.insert(tk.END, f"\nАгрегаційний скрипт перезапущено з новим сервером оцінки ({self.evaluation_server_ip})\n")
                self.metrics_text.config(state=tk.DISABLED)

            except Exception as e:
                self.metrics_text.config(state=tk.NORMAL)
                self.metrics_text.insert(tk.END, f"\nПомилка при оновленні агрегаційного скрипту: {str(e)}\n")
                self.metrics_text.config(state=tk.DISABLED)

def kill_process_tree(pid):
    """Функція для завершення процесу та всіх його дочірніх процесів"""
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            child.terminate()
        parent.terminate()
    except psutil.NoSuchProcess:
        pass

if __name__ == "__main__":
    root = tk.Tk()
    app = FederatedSystemGUI(root)
    root.mainloop()