import tensorflow as tf
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import LSTM, Dense, Input, Bidirectional, GRU
import numpy as np
from scipy.ndimage import uniform_filter1d
import os

# Константи
INPUT_SIZE = 150
OUTPUT_SIZE = 6
FEATURES = 6

class SignalPredictor(tf.keras.Model):
    def __init__(self):
        super().__init__()
        self.model = Sequential(name="signal_predictor")
        self.model.add(Input(shape=(INPUT_SIZE, FEATURES), name="input_layer"))
        self.model.add(Bidirectional(
            GRU(96, return_sequences=True, trainable=True, name="forward_gru_1"),
            name="bidirectional_gru_1"
        ))
        self.model.add(Bidirectional(
            GRU(64, trainable=True, name="forward_gru_2"),
            name="bidirectional_gru_2"
        ))
        self.model.add(Dense(32, activation="relu", trainable=True, name="dense_1"))
        self.model.add(Dense(FEATURES, trainable=True, name="dense_2"))
        self.model.compile(optimizer="adam", loss=tf.keras.losses.MeanSquaredError())
        self.model.summary()

    @tf.function(
        input_signature=[
            tf.TensorSpec([None, INPUT_SIZE, FEATURES], tf.float32),
            tf.TensorSpec([None, FEATURES], tf.float32),
        ]
    )
    def train(self, x, y):
        with tf.GradientTape() as tape:
            prediction = self.model(x)
            loss = self.model.loss(y, prediction)
        gradients = tape.gradient(loss, self.model.trainable_variables)
        self.model.optimizer.apply_gradients(
            zip(gradients, self.model.trainable_variables)
        )
        return {"loss": loss}

    @tf.function(
        input_signature=[tf.TensorSpec([None, INPUT_SIZE, FEATURES], tf.float32)]
    )
    def infer(self, x):
        prediction = self.model(x)
        return {"output": prediction}

    @tf.function(input_signature=[tf.TensorSpec(shape=[], dtype=tf.string)])
    def save(self, checkpoint_path):
        tensor_names = [weight.name for weight in self.model.weights]
        tensors_to_save = [weight for weight in self.model.weights]
        print("Зберігаємо тензори:", tensor_names)

        tf.raw_ops.Save(
            filename=checkpoint_path,
            tensor_names=tensor_names,
            data=tensors_to_save,
            name="save",
        )
        return {"checkpoint_path": checkpoint_path}

    @tf.function(input_signature=[tf.TensorSpec(shape=[], dtype=tf.string)])
    def restore(self, checkpoint_path):
        restored_tensors = {}
        for var in self.model.weights:
            restored = tf.raw_ops.Restore(
                file_pattern=checkpoint_path,
                tensor_name=var.name,
                dt=var.dtype,
                name='restore'
            )
            restored = tf.ensure_shape(restored, var.shape)
            var.assign(restored)
            restored_tensors[var.name] = restored
        return restored_tensors

def apply_moving_average(data, window_size=5):
    """Застосування усереднювального вікна до даних
    
    Args:
        data (np.ndarray): вхідні дані форми (n_samples, n_features)
        window_size (int): розмір вікна усереднення
    
    Returns:
        np.ndarray: оброблені дані
    """
    smoothed_data = np.zeros_like(data)
    for i in range(data.shape[1]):
        smoothed_data[:, i] = uniform_filter1d(data[:, i], size=window_size, mode='nearest')
    return smoothed_data

def load_data(filepath, input_size, output_size, min_vals=None, max_vals=None):
    """Завантаження та підготовка даних
    
    Args:
        filepath (str): шлях до файлу з даними
        input_size (int): розмір вхідного вікна
        output_size (int): розмір вихідного вікна
        min_vals (np.ndarray, optional): мінімальні значення для нормалізації
        max_vals (np.ndarray, optional): максимальні значення для нормалізації
    """
    with open(filepath, "r") as f:
        lines = f.readlines()

    acc_data, gyro_data = [], []
    reading_gyro = False
    for line in lines:
        line = line.strip()
        if not line or "Accelerometer" in line:
            continue
        if "Gyroscope" in line:
            reading_gyro = True
            continue
        values = list(map(float, line.split(",")))
        if reading_gyro:
            gyro_data.append(values)
        else:
            acc_data.append(values)

    min_length = min(len(acc_data), len(gyro_data))
    acc_data = np.array(acc_data[:min_length])
    gyro_data = np.array(gyro_data[:min_length])

    # Застосування усереднювального вікна до даних
    acc_data = apply_moving_average(acc_data, window_size=5)
    gyro_data = apply_moving_average(gyro_data, window_size=5)

    data = np.hstack([acc_data, gyro_data])

    # Нормалізація даних
    if min_vals is None or max_vals is None:
        min_vals = np.min(data, axis=0)
        max_vals = np.max(data, axis=0)
        # Зберігаємо значення тільки якщо вони були обчислені
        np.save("min_vals.npy", min_vals)
        np.save("max_vals.npy", max_vals)

        min_vals_str = ",".join(map(str, min_vals))
        max_vals_str = ",".join(map(str, max_vals))
        with open(os.path.join("min_vals.txt"), "w") as f_min:
            f_min.write(min_vals_str)
        with open(os.path.join("max_vals.txt"), "w") as f_max:
            f_max.write(max_vals_str)
        print(f"Saved new min/max normalization parameters to min_vals.txt and max_vals.txt")

    data = (data - min_vals) / (max_vals - min_vals + 1e-8)

    X, Y = [], []
    for i in range(len(data) - input_size - output_size):
        X.append(data[i : i + input_size])
        Y.append(data[i + input_size])

    return np.array(X, dtype=np.float32), np.array(Y, dtype=np.float32)


def load_and_prepare_test_data(filepath):
    """Завантаження та підготовка тестових даних"""
    try:
        min_vals = np.load("testing_data/min_vals.npy")
        max_vals = np.load("testing_data/max_vals.npy")
    except FileNotFoundError:
        print("Файли з значеннями нормалізації не знайдено. Буде використано значення з тестових даних")
        min_vals = None
        max_vals = None

    X_test, y_test = load_data(filepath, INPUT_SIZE, 1, min_vals, max_vals)
    return X_test, y_test