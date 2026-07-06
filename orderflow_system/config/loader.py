"""
Central config loader. Every module in this system pulls settings from here
instead of hardcoding values, so you only ever edit config.yaml.
"""
import yaml
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config(path: str | None = None) -> dict:
    cfg_path = Path(path) if path else _CONFIG_PATH
    with open(cfg_path, "r") as f:
        return yaml.safe_load(f)


if __name__ == "__main__":
    cfg = load_config()
    print("Loaded config for symbols:", [s["name"] for s in cfg["symbols"]])
    print("Strategy:", cfg["strategy"]["name"])
    print("Decision engine provider:", cfg["decision_engine"]["provider"])
