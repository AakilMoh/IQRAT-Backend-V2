import torch
import torch.nn as nn
import numpy as np
from sklearn.ensemble import RandomForestRegressor

# ==========================================
# 1. PyTorch LSTM MODEL (Attendance Forecasting)
# ==========================================
class AttendanceLSTM(nn.Module):
    def __init__(self, input_size=1, hidden_layer_size=16, output_size=1):
        super().__init__()
        self.hidden_layer_size = hidden_layer_size
        self.lstm = nn.LSTM(input_size, hidden_layer_size, batch_first=True)
        self.linear = nn.Linear(hidden_layer_size, output_size)

    def forward(self, input_seq):
        # LSTM outputs a sequence, we only want the prediction from the final time step
        lstm_out, _ = self.lstm(input_seq)
        predictions = self.linear(lstm_out[:, -1, :]) 
        # Use Sigmoid to force the output between 0 and 1 (a percentage)
        return torch.sigmoid(predictions) 

# ==========================================
# 2. AI PREDICTOR ENGINE
# ==========================================
class StudentAIPredictor:
    def __init__(self):
        # Initialize our PyTorch Neural Network
        self.attendance_model = AttendanceLSTM()
        # Initialize our Scikit-Learn Machine Learning Model
        self.grade_model = RandomForestRegressor(n_estimators=100, random_state=42)
        self._is_rf_trained = False

    def predict_attendance(self, attendance_sequence):
        """
        Expects a list of 1s (Present) and 0s (Absent/Late). e.g., [1, 1, 0, 1]
        Returns the forecasted final attendance percentage.
        """
        if not attendance_sequence:
            return 0.0

        # Pad or truncate the sequence to a fixed window (e.g., look at the last 10 classes)
        seq = np.array(attendance_sequence, dtype=np.float32)
        if len(seq) < 10:
            seq = np.pad(seq, (10 - len(seq), 0), 'constant', constant_values=1.0)
        else:
            seq = seq[-10:]

        # Convert to PyTorch tensor format: (batch_size, sequence_length, features)
        seq_tensor = torch.FloatTensor(seq).unsqueeze(0).unsqueeze(-1)
        
        self.attendance_model.eval()
        with torch.no_grad():
            pred = self.attendance_model(seq_tensor)
        
        return float(pred.item()) * 100 # Convert back to a 0-100 percentage

    def predict_grade(self, sessional_obtained, sessional_max, predicted_attendance):
        """
        Predicts the FINAL EXAM score (out of 50) based on Sessional performance and attendance.
        """
        if not self._is_rf_trained:
            # --- DUMMY TRAINING BOOTSTRAP FOR 50/50 SPLIT ---
            # Input: [Sessional Obtained, Sessional Max, Predicted Attendance %]
            # Output: Predicted Final Exam Score (out of 50)
            X_dummy = np.array([[45, 50, 95], [20, 50, 60], [30, 50, 80], [10, 50, 40], [26, 50, 75]])
            y_dummy = np.array([42, 18, 35, 12, 26]) # Output is STRICTLY out of 50
            self.grade_model.fit(X_dummy, y_dummy)
            self._is_rf_trained = True

        X_input = np.array([[sessional_obtained, sessional_max, predicted_attendance]])
        pred_final_exam = self.grade_model.predict(X_input)
        
        # Cap the prediction between 0 and 50
        return max(0.0, min(50.0, float(pred_final_exam[0])))

# Expose a singleton instance to be used across the app
ai_engine = StudentAIPredictor()