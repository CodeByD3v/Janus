"""
evals/eval_byok.py — Tests for Phase 5 BYOK multi-provider features.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import ModelConfig, SUPPORTED_PROVIDERS, settings
from core.llm_client import build_model_for_config
from core.repo_config import RepoConfig
from api.schemas import CreateDebateRequest

# ---------------------------------------------------------------------------
# 1. ModelConfig dataclass
# ---------------------------------------------------------------------------

def test_model_config_defaults():
    config = ModelConfig()
    assert config.provider == "google"
    assert config.model == ""
    assert config.api_key == ""

def test_model_config_effective_model():
    config = ModelConfig(model="gpt-4o")
    assert config.effective_model("default-model") == "gpt-4o"
    
    empty_config = ModelConfig()
    assert empty_config.effective_model("default-model") == "default-model"

def test_model_config_is_google():
    assert ModelConfig(provider="google").is_google is True
    assert ModelConfig(provider="openai").is_google is False

# ---------------------------------------------------------------------------
# 2. SUPPORTED_PROVIDERS
# ---------------------------------------------------------------------------

def test_supported_providers():
    assert isinstance(SUPPORTED_PROVIDERS, frozenset)
    for provider in ["google", "openai", "anthropic", "groq"]:
        assert provider in SUPPORTED_PROVIDERS

# ---------------------------------------------------------------------------
# 3. Settings.byok_api_key
# ---------------------------------------------------------------------------

def test_settings_byok_api_key_from_model_config():
    config = ModelConfig(provider="openai", api_key="user-provided-key")
    assert settings.byok_api_key(config) == "user-provided-key"

def test_settings_byok_api_key_fallback_to_env():
    # Use dataclasses.replace to create a mock settings object
    mock_settings = dataclasses.replace(settings, OPENAI_API_KEY="server-openai-key")
    config = ModelConfig(provider="openai")
    
    assert mock_settings.byok_api_key(config) == "server-openai-key"

def test_settings_byok_api_key_unknown_provider():
    config = ModelConfig(provider="unknown")
    assert settings.byok_api_key(config) == ""

# ---------------------------------------------------------------------------
# 4. build_model_for_config
# ---------------------------------------------------------------------------

@patch("core.llm_client.build_model")
def test_build_model_for_config_google(mock_build_model):
    mock_build_model.return_value = (MagicMock(), 0)
    config = ModelConfig(provider="google", model="gemini-2.5-flash")
    
    model, index = build_model_for_config(config)
    
    mock_build_model.assert_called_once_with("gemini-2.5-flash")
    assert index == 0

def test_build_model_for_config_litellm_missing():
    config = ModelConfig(provider="openai", model="gpt-4o")
    
    # Hide google.adk.models.LiteLlm
    with patch.dict(sys.modules, {"google.adk.models": None}):
        with pytest.raises(RuntimeError, match="LiteLlm is required for provider 'openai'"):
            build_model_for_config(config)

def test_build_model_for_config_no_api_key(monkeypatch):
    config = ModelConfig(provider="openai", model="gpt-4o")
    
    # Replace the frozen settings object rather than assigning to one of its
    # methods, which would fail during monkeypatch teardown.
    monkeypatch.setattr(
        "core.llm_client.settings",
        dataclasses.replace(settings, OPENAI_API_KEY=""),
    )
    
    # Mock litellm to avoid import error
    mock_adk_models = MagicMock()
    mock_adk_models.LiteLlm = MagicMock()
    with patch.dict(sys.modules, {"google.adk.models": mock_adk_models}):
        with pytest.raises(RuntimeError, match="No API key available for provider 'openai'"):
            build_model_for_config(config)

def test_build_model_for_config_success():
    config = ModelConfig(provider="openai", model="gpt-4o", api_key="my-key")
    
    # Mock Litellm
    mock_litellm_instance = MagicMock()
    mock_litellm_class = MagicMock(return_value=mock_litellm_instance)
    
    mock_adk_models = MagicMock()
    mock_adk_models.LiteLlm = mock_litellm_class
    
    with patch.dict(sys.modules, {"google.adk.models": mock_adk_models}):
        model, index = build_model_for_config(config)
        
        assert model is mock_litellm_instance
        assert index == -1
        mock_litellm_class.assert_called_once_with(model="openai/gpt-4o", api_key="my-key")

# ---------------------------------------------------------------------------
# 5. RepoConfig model parsing
# ---------------------------------------------------------------------------

def test_repo_config_to_model_config_none():
    repo = RepoConfig()
    assert repo.to_model_config() is None

def test_repo_config_to_model_config_set():
    repo = RepoConfig(model_provider="openai", model_name="gpt-4o")
    config = repo.to_model_config()
    
    assert config is not None
    assert config.provider == "openai"
    assert config.model == "gpt-4o"

def test_repo_config_from_yaml(tmp_path):
    yaml_content = """
model:
  provider: anthropic
  name: claude-3-opus
"""
    yaml_file = tmp_path / "janus.yaml"
    yaml_file.write_text(yaml_content)
    
    repo = RepoConfig.from_yaml(yaml_file)
    assert repo.model_provider == "anthropic"
    assert repo.model_name == "claude-3-opus"

# ---------------------------------------------------------------------------
# 6. API schema validation
# ---------------------------------------------------------------------------

@patch("api.schemas.validate_repo_ref")
def test_create_debate_request_valid_provider(mock_validate_repo_ref):
    req = CreateDebateRequest(
        repo_ref="owner/repo",
        target_file="file.py",
        ticket="fix it",
        model_provider="openai",
        model_name="gpt-4o"
    )
    assert req.model_provider == "openai"
    assert req.model_name == "gpt-4o"

@patch("api.schemas.validate_repo_ref")
def test_create_debate_request_invalid_provider(mock_validate_repo_ref):
    with pytest.raises(ValidationError, match="Unsupported provider 'invalid-provider'"):
        CreateDebateRequest(
            repo_ref="owner/repo",
            target_file="file.py",
            ticket="fix it",
            model_provider="invalid-provider"
        )
