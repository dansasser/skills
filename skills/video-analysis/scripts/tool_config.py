"""Private tool settings. Only an explicit credential lookup reads the dotenv file."""
import json
import os
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    pass


def _section(document, name, allowed):
    value = document.get(name, {})
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ConfigError(f"Invalid {name} section or unknown setting")
    return value


def _string(value, field):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ConfigError(f"{field} must be a nonempty string")
    return value


def _path(base, value):
    path = Path(value).expanduser()
    return str((base / path).resolve())


def _executable(base, value, field):
    value = _string(value, field)
    if '/' in value or '\\' in value or value.startswith('~'):
        return _path(base, value)
    return value


def load_config(selected=None):
    location = selected or os.environ.get("VIDEO_ANALYSIS_CONFIG")
    base = Path.cwd()
    document = {}
    if location:
        try:
            path = Path(location).expanduser().absolute()
            document = json.loads(path.read_text(encoding="utf-8"))
            base = path.parent
        except (OSError, UnicodeError, ValueError) as exc:
            raise ConfigError("Cannot read the selected config as UTF-8 JSON") from exc
        if (not isinstance(document, dict) or type(document.get("version")) is not int
                or document["version"] != 1):
            raise ConfigError("The selected config must have integer version 1")
        if set(document) - {"version", "tools", "asr", "tts", "env_file"}:
            raise ConfigError("Unknown top-level config setting")
    tools = _section(document, "tools", {"ffmpeg", "ffprobe"})
    result = {"tools": {k: _executable(base, tools.get(k, k), f"tools.{k}") for k in ("ffmpeg", "ffprobe")},
              "asr": None, "tts": None, "env_file": None}
    if "env_file" in document:
        result["env_file"] = _path(base, _string(document["env_file"], "env_file"))
    asr = _section(document, "asr", {"backend", "executable", "model_path", "language", "threads"})
    if asr:
        if asr.get("backend", "whisper.cpp") != "whisper.cpp":
            raise ConfigError("asr.backend must be whisper.cpp")
        threads = asr.get("threads", 4)
        if type(threads) is not int or threads < 1:
            raise ConfigError("asr.threads must be a positive integer")
        result["asr"] = {
            "executable": _executable(base, asr.get("executable", "whisper-cli"), "asr.executable"),
            "model_path": _path(base, _string(asr["model_path"], "asr.model_path")) if "model_path" in asr else None,
            "language": _string(asr.get("language", "auto"), "asr.language"),
            "threads": threads,
        }
    tts = _section(document, "tts", {"base_url", "model", "api_key_env"})
    if tts:
        url = _string(tts.get("base_url"), "tts.base_url")
        try:
            parts = urlsplit(url)
            valid = parts.scheme in ("http", "https") and bool(parts.hostname)
        except ValueError:
            valid = False
        if not valid or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
            raise ConfigError("tts.base_url must be an HTTP(S) base URL without embedded credentials, query or fragment")
        key_name = tts.get("api_key_env")
        if key_name is not None and (not isinstance(key_name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key_name)):
            raise ConfigError("tts.api_key_env must name an environment variable")
        result["tts"] = {"base_url": url, "model": _string(tts.get("model"), "tts.model"), "api_key_env": key_name}
    return result


def tts_key(config):
    """Resolve just the named value. Never export, execute, interpolate or display it."""
    name = (config["tts"] or {}).get("api_key_env")
    if not name:
        return None
    if name in os.environ:
        return os.environ[name] or None
    filename = config["env_file"]
    if not filename:
        return None
    try:
        lines = Path(filename).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise ConfigError("Cannot read the configured dotenv file") from exc
    value = None
    for line in lines:
        match = re.fullmatch(r"\s*(?:export\s+)?" + re.escape(name) + r"\s*=\s*(.*)", line)
        if match:
            raw = match[1].strip()
            if raw.startswith(('"', "'")):
                quoted = re.fullmatch(r"""(["'])(.*?)\1\s*(?:#.*)?""", raw)
                if not quoted:
                    raise ConfigError(f"Invalid quoted dotenv value for {name}")
                value = quoted[2]
            else:
                value = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
    return value or None


def config_summary(config):
    asr = config["asr"]
    return {
        "tools": {key: {"executable": name, "available": shutil.which(name) is not None}
                  for key, name in config["tools"].items()},
        "asr": None if asr is None else {**asr, "executable_available": bool(shutil.which(asr["executable"])),
                                         "model_available": bool(asr["model_path"] and Path(asr["model_path"]).is_file())},
        "tts": {**config["tts"], "key_available": bool(tts_key(config))} if config["tts"] else None,
    }
