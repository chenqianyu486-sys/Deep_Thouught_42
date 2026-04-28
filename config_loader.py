"""Configuration loader for LLM model settings.

Loads model configuration from model_config.yaml and provides
ModelContextConfig instances for different model tiers.
"""

import os
import yaml
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

# Default configuration path
DEFAULT_CONFIG_PATH = Path(__file__).parent / "model_config.yaml"


@dataclass
class ModelConfigData:
    """Raw model configuration data from YAML."""
    model_tier: str
    model_name: str
    max_tokens: int
    soft_threshold: int
    hard_limit: int
    token_budget: int
    preserve_turns: int
    preserve_turns_aggressive: int = 3  # Default for aggressive mode
    cost_hard_limit: float = 1.0  # USD hard limit (combined planner+worker budget)
    min_importance_threshold: float = 0.3
    min_importance_threshold_aggressive: float = 0.8  # Default for aggressive mode
    history_retrieval_limit: int = 5
    history_retrieval_min_importance: float = 0.6
    fallback_models: list[str] = None  # Fallback models for 429 handling

    def __post_init__(self):
        if self.fallback_models is None:
            self.fallback_models = []


class ModelConfigLoader:
    """Loads and manages model configuration from YAML file."""

    _instance: Optional['ModelConfigLoader'] = None
    _flash_config: Optional[ModelConfigData] = None
    _pro_config: Optional[ModelConfigData] = None
    _config_path: Optional[Path] = None

    def __new__(cls, config_path: Optional[Path] = None):
        """Singleton pattern to ensure config is loaded once."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_path: Optional[Path] = None):
        """Initialize config loader.

        Args:
            config_path: Path to model_config.yaml. Defaults to project root.
        """
        if config_path is None:
            config_path = DEFAULT_CONFIG_PATH

        # Only reload if path changed
        if self._config_path != config_path:
            self._config_path = config_path
            self._load_config()

    def _load_config(self) -> None:
        """Load configuration from YAML file."""
        if not self._config_path or not self._config_path.exists():
            raise FileNotFoundError(f"Model config file not found: {self._config_path}")

        with open(self._config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        if 'flash' not in config or 'pro' not in config:
            raise ValueError("Config must contain 'flash' and 'pro' sections")

        self._flash_config = ModelConfigData(
            model_tier='flash',
            model_name=config['flash'].get('model_name', 'xiaomi/mimo-v2-flash'),
            max_tokens=config['flash']['max_tokens'],
            soft_threshold=config['flash']['soft_threshold'],
            hard_limit=config['flash']['hard_limit'],
            token_budget=config['flash']['token_budget'],
            preserve_turns=config['flash']['preserve_turns'],
            preserve_turns_aggressive=config['flash'].get('preserve_turns_aggressive', 3),
            cost_hard_limit=config['flash'].get('cost_hard_limit', 1.0),
            min_importance_threshold=config['flash']['min_importance_threshold'],
            min_importance_threshold_aggressive=config['flash'].get('min_importance_threshold_aggressive', 0.8),
            history_retrieval_limit=config['flash']['history_retrieval_limit'],
            history_retrieval_min_importance=config['flash']['history_retrieval_min_importance'],
            fallback_models=config['flash'].get('fallback_models', []),
        )

        self._pro_config = ModelConfigData(
            model_tier='pro',
            model_name=config['pro'].get('model_name', 'xiaomi/mimo-v2-pro'),
            max_tokens=config['pro']['max_tokens'],
            soft_threshold=config['pro']['soft_threshold'],
            hard_limit=config['pro']['hard_limit'],
            token_budget=config['pro']['token_budget'],
            preserve_turns=config['pro']['preserve_turns'],
            preserve_turns_aggressive=config['pro'].get('preserve_turns_aggressive', 3),
            cost_hard_limit=config['pro'].get('cost_hard_limit', 1.0),
            min_importance_threshold=config['pro']['min_importance_threshold'],
            min_importance_threshold_aggressive=config['pro'].get('min_importance_threshold_aggressive', 0.8),
            history_retrieval_limit=config['pro']['history_retrieval_limit'],
            history_retrieval_min_importance=config['pro']['history_retrieval_min_importance'],
            fallback_models=config['pro'].get('fallback_models', []),
        )

    def get_flash_config(self) -> ModelConfigData:
        """Get Flash model configuration."""
        if self._flash_config is None:
            self._load_config()
        return self._flash_config

    def get_pro_config(self) -> ModelConfigData:
        """Get Pro model configuration."""
        if self._pro_config is None:
            self._load_config()
        return self._pro_config

    def get_config(self, model_tier: str) -> ModelConfigData:
        """Get configuration by model tier.

        Args:
            model_tier: "flash" or "pro"

        Returns:
            ModelConfigData for the specified tier
        """
        if model_tier == "flash":
            return self.get_flash_config()
        elif model_tier == "pro":
            return self.get_pro_config()
        else:
            raise ValueError(f"Unknown model tier: {model_tier}. Use 'flash' or 'pro'.")

    def reload(self, config_path: Optional[Path] = None) -> None:
        """Reload configuration from file.

        Args:
            config_path: Optional new path to config file
        """
        if config_path is not None:
            self._config_path = config_path
        self._flash_config = None
        self._pro_config = None
        self._load_config()


# Convenience functions
_loader: Optional[ModelConfigLoader] = None


def get_model_config_loader(config_path: Optional[Path] = None) -> ModelConfigLoader:
    """Get or create the model config loader singleton.

    Args:
        config_path: Optional path to config file

    Returns:
        ModelConfigLoader instance
    """
    global _loader
    if _loader is None:
        _loader = ModelConfigLoader(config_path)
    elif config_path is not None:
        _loader.reload(config_path)
    return _loader


def get_flash_model_config() -> ModelConfigData:
    """Get Flash model configuration."""
    return get_model_config_loader().get_flash_config()


def get_pro_model_config() -> ModelConfigData:
    """Get Pro model configuration."""
    return get_model_config_loader().get_pro_config()
