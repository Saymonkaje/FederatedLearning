import tensorflow as tf
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import LSTM, Dense, Input, Bidirectional, GRU

INPUT_SIZE = 150
OUTPUT_SIZE = 6
FEATURES = 6

class SignalPredictor(tf.keras.Model):

    def __init__(self):
        super().__init__()
        # self.model = Sequential(name="signal_predictor")
        # self.model.add(Input(shape=(INPUT_SIZE, FEATURES), name="input_layer"))
        # self.model.add(LSTM(64, return_sequences=True, trainable=True, name="lstm_1"))
        # self.model.add(LSTM(32,sds trainable=True, name="lstm_2"))
        # self.model.add(Dense(32, activation="relu", trainable=True, name="dense_1"))
        # self.model.add(Dense(FEATURES, trainable=True, name="dense_2"))
        # self.model.compile(optimizer="adam", loss=tf.keras.losses.MeanSquaredError())
        # self.model.summary()
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