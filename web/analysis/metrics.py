"""M2 - Lógica: cálculo de EAR y MAR (RF-03) a partir de puntos en píxeles."""

import numpy as np

# Índices de la malla de 478 puntos, en orden p1..p6.
RIGHT_EYE = (33, 160, 158, 133, 153, 144)
LEFT_EYE = (362, 385, 387, 263, 373, 380)

MOUTH_CORNERS = (61, 291)
MOUTH_VERTICAL_PAIRS = ((81, 178), (13, 14), (311, 402))


def _distance(point_a, point_b):
    return float(np.linalg.norm(point_a - point_b))


def eye_aspect_ratio(landmarks, eye):
    """EAR = (|p2-p6| + |p3-p5|) / (2 * |p1-p4|)."""
    p1, p2, p3, p4, p5, p6 = (landmarks[index] for index in eye)
    width = _distance(p1, p4)
    if width == 0:
        return 0.0
    return (_distance(p2, p6) + _distance(p3, p5)) / (2 * width)


def compute_ear(landmarks):
    """EAR promedio de ambos ojos."""
    return (eye_aspect_ratio(landmarks, RIGHT_EYE) + eye_aspect_ratio(landmarks, LEFT_EYE)) / 2


def compute_mar(landmarks):
    """MAR = promedio de las distancias verticales de la boca / ancho de la boca."""
    left_corner, right_corner = MOUTH_CORNERS
    width = _distance(landmarks[left_corner], landmarks[right_corner])
    if width == 0:
        return 0.0
    verticals = [_distance(landmarks[top], landmarks[bottom]) for top, bottom in MOUTH_VERTICAL_PAIRS]
    return sum(verticals) / len(verticals) / width
