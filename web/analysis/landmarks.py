"""M2 - Infraestructura: puntos faciales con MediaPipe (Tasks API).

No se entrena ningún modelo: se usa el de MediaPipe, ya entrenado. Las imágenes
sólo se procesan en memoria y nunca se guardan (RNF-05).
"""

import urllib.error
import urllib.request
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions, vision

MODEL_URL = (
    'https://storage.googleapis.com/mediapipe-models/face_landmarker/'
    'face_landmarker/float16/1/face_landmarker.task'
)
# No se sube a Git: se descarga la primera vez que se necesita.
MODEL_PATH = Path(__file__).resolve().parent / 'models' / 'face_landmarker.task'


class LandmarkError(Exception):
    """No se pudo obtener o cargar el modelo de MediaPipe."""


def ensure_model(path=MODEL_PATH):
    """Devuelve la ruta del modelo y lo descarga si todavía no existe."""
    path = Path(path)
    if path.exists():
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    # Se descarga a un archivo temporal para no dejar un modelo a medias si se corta.
    partial = path.with_name(path.name + '.part')
    try:
        urllib.request.urlretrieve(MODEL_URL, partial)
        partial.replace(path)
    except (urllib.error.URLError, OSError) as error:
        raise LandmarkError(
            f'No se pudo descargar el modelo de MediaPipe. Revise su conexión a Internet. ({error})'
        ) from error
    finally:
        partial.unlink(missing_ok=True)
    return path


def to_pixels(face_landmarks, width, height):
    """Convierte los puntos normalizados de MediaPipe a píxeles (x*ancho, y*alto)."""
    return np.array([(point.x * width, point.y * height) for point in face_landmarks])


class FaceLandmarkDetector:
    """Detecta un rostro por fotograma. Crear y usar siempre desde el mismo hilo."""

    def __init__(self, model_path=None):
        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(ensure_model(model_path or MODEL_PATH))),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
        )
        self._landmarker = vision.FaceLandmarker.create_from_options(options)

    def detect(self, image, timestamp_ms):
        """Devuelve los puntos del rostro en píxeles, o None si no hay rostro.

        `image` es BGR, y `timestamp_ms` debe ser estrictamente creciente.
        """
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = self._landmarker.detect_for_video(mp_image, timestamp_ms)
        if not result.face_landmarks:
            return None
        height, width = image.shape[:2]
        return to_pixels(result.face_landmarks[0], width, height)

    def close(self):
        self._landmarker.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
