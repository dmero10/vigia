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

# Se buscan varios rostros para saber cuál se mide y avisar si hay más de uno.
MAX_FACES = 3


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


def face_box(points):
    """Recuadro (x0, y0, x1, y1) en píxeles que encierra los puntos de un rostro."""
    x0, y0 = points.min(axis=0)
    x1, y1 = points.max(axis=0)
    return float(x0), float(y0), float(x1), float(y1)


def box_area(box):
    x0, y0, x1, y1 = box
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def largest_face(faces):
    """Índice del rostro más grande (el más cercano a la cámara), o None."""
    if not faces:
        return None
    return max(range(len(faces)), key=lambda index: box_area(face_box(faces[index])))


class FaceLandmarkDetector:
    """Detecta hasta MAX_FACES rostros por fotograma. Crear y usar desde el mismo hilo."""

    def __init__(self, model_path=None, max_faces=MAX_FACES):
        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(ensure_model(model_path or MODEL_PATH))),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=max_faces,
        )
        self._landmarker = vision.FaceLandmarker.create_from_options(options)

    def detect_all(self, image, timestamp_ms):
        """Devuelve los puntos de cada rostro en píxeles (lista vacía si no hay).

        `image` es BGR, y `timestamp_ms` debe ser estrictamente creciente.
        """
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = self._landmarker.detect_for_video(mp_image, timestamp_ms)
        height, width = image.shape[:2]
        return [to_pixels(face, width, height) for face in result.face_landmarks]

    def detect(self, image, timestamp_ms):
        """Puntos del rostro más grande en píxeles, o None si no hay rostro."""
        faces = self.detect_all(image, timestamp_ms)
        index = largest_face(faces)
        return None if index is None else faces[index]

    def close(self):
        self._landmarker.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
