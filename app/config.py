"""Application configuration, driven entirely by environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class Config:
    """Base configuration shared by every environment."""

    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-insecure-key")

    # --- Database ---------------------------------------------------------
    MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
    MYSQL_PORT = _int("MYSQL_PORT", 3306)
    MYSQL_USER = os.getenv("MYSQL_USER", "clocking")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "clocking")

    # TLS to the database. "" means "decide from the host" - see mysql_ssl_mode.
    MYSQL_SSL_MODE = (os.getenv("MYSQL_SSL_MODE") or "").strip().lower()
    MYSQL_SSL_CA = (os.getenv("MYSQL_SSL_CA") or "").strip()

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # --- Face models ------------------------------------------------------
    MODEL_DIR = BASE_DIR / "models"
    FACE_DETECTOR_MODEL = MODEL_DIR / "face_detection_yunet_2023mar.onnx"
    FACE_RECOGNISER_MODEL = MODEL_DIR / "face_recognition_sface_2021dec.onnx"

    # --- Recognition tuning ----------------------------------------------
    FACE_MATCH_THRESHOLD = _float("FACE_MATCH_THRESHOLD", 0.40)
    FACE_MATCH_MARGIN = _float("FACE_MATCH_MARGIN", 0.05)
    FACE_MIN_PIXELS = _int("FACE_MIN_PIXELS", 80)
    FACE_MIN_SHARPNESS = _float("FACE_MIN_SHARPNESS", 45.0)
    FACE_DETECT_MAX_SIDE = _int("FACE_DETECT_MAX_SIDE", 640)
    FACE_DETECT_CONFIDENCE = _float("FACE_DETECT_CONFIDENCE", 0.85)

    SCAN_FRAMES = _int("SCAN_FRAMES", 3)
    SCAN_MIN_AGREE = _int("SCAN_MIN_AGREE", 2)
    ENROL_MIN_SAMPLES = _int("ENROL_MIN_SAMPLES", 3)
    ENROL_MAX_SAMPLES = _int("ENROL_MAX_SAMPLES", 6)

    LIVENESS_REQUIRE_MOTION = _bool("LIVENESS_REQUIRE_MOTION", True)
    LIVENESS_MIN_MOTION = _float("LIVENESS_MIN_MOTION", 1.6)

    # --- Attendance rules -------------------------------------------------
    CLOCK_COOLDOWN_SECONDS = _int("CLOCK_COOLDOWN_SECONDS", 90)
    TIMEZONE = os.getenv("TIMEZONE", "Europe/London")

    # --- Kiosk ------------------------------------------------------------
    KIOSK_TOKEN = os.getenv("KIOSK_TOKEN", "")
    KIOSK_DEVICE_LABEL = os.getenv("KIOSK_DEVICE_LABEL", "Kiosk")

    # --- Uploads / limits -------------------------------------------------
    # A scan posts a handful of JPEG frames; 12 MB is generous headroom.
    MAX_CONTENT_LENGTH = 12 * 1024 * 1024
    RECOGNISE_RATE_LIMIT = _int("RECOGNISE_RATE_LIMIT", 30)
    RECOGNISE_RATE_WINDOW = _int("RECOGNISE_RATE_WINDOW", 60)
    LOGIN_RATE_LIMIT = _int("LOGIN_RATE_LIMIT", 10)
    LOGIN_RATE_WINDOW = _int("LOGIN_RATE_WINDOW", 300)

    # --- Session hardening ------------------------------------------------
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False  # switched on in ProductionConfig
    REMEMBER_COOKIE_HTTPONLY = True
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12

    LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:  # noqa: N802 - Flask config name
        return (
            f"mysql+pymysql://{quote_plus(self.MYSQL_USER)}:"
            f"{quote_plus(self.MYSQL_PASSWORD)}@{self.MYSQL_HOST}:{self.MYSQL_PORT}/"
            f"{self.MYSQL_DATABASE}?charset=utf8mb4"
        )

    @property
    def mysql_ssl_mode(self) -> str:
        """One of "disabled", "required" or "verify-identity".

        Defaults by host: a database on this machine needs no TLS, but anything
        reached over a network does - and a managed database (DigitalOcean, RDS,
        Azure) is reached across the public internet. Face templates are
        biometric data, so shipping them unencrypted is not an option.
        """
        if self.MYSQL_SSL_MODE in {"disabled", "required", "verify-identity"}:
            return self.MYSQL_SSL_MODE
        return "disabled" if self.MYSQL_HOST in self.LOCAL_HOSTS else "verify-identity"

    @property
    def mysql_connect_args(self) -> dict:
        """PyMySQL connect arguments implementing :attr:`mysql_ssl_mode`.

        Only these exact options actually negotiate TLS. Several plausible
        alternatives - ``ssl={}``, ``ssl_verify_cert=False``,
        ``ssl_disabled=False`` - connect in *plaintext* while looking as though
        they enabled encryption, so do not "simplify" this without checking
        ``SHOW STATUS LIKE 'Ssl_version'`` on a real connection afterwards.
        """
        mode = self.mysql_ssl_mode
        if mode == "disabled":
            return {}

        args: dict = {"ssl": {"ssl": True}}
        if mode == "verify-identity":
            args["ssl_verify_cert"] = True
            args["ssl_verify_identity"] = True
            if self.MYSQL_SSL_CA:
                args["ssl_ca"] = self.MYSQL_SSL_CA
        return args

    @property
    def SQLALCHEMY_ENGINE_OPTIONS(self) -> dict:  # noqa: N802 - Flask config name
        options: dict = {
            # Long-lived kiosks sit idle overnight; recycle before the server's
            # wait_timeout drops the connection underneath us. Managed databases
            # often use a much shorter timeout than MySQL's 8-hour default.
            "pool_pre_ping": True,
            "pool_recycle": 3600,
        }
        if self.SQLALCHEMY_DATABASE_URI.startswith("mysql"):
            connect_args = self.mysql_connect_args
            if connect_args:
                options["connect_args"] = connect_args
        return options


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    KIOSK_TOKEN = "test-kiosk-token"
    SECRET_KEY = "test-secret-key"

    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:  # noqa: N802 - Flask config name
        return os.getenv("TEST_DATABASE_URI", "sqlite+pysqlite:///:memory:")


CONFIGS = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestConfig,
}


def get_config(name: str | None = None) -> Config:
    """Return a config instance for *name*, falling back to FLASK_ENV."""
    key = (name or os.getenv("FLASK_ENV") or "development").strip().lower()
    return CONFIGS.get(key, DevelopmentConfig)()
