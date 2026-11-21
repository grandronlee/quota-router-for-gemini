import pytest

from quota_router.config import ConfigError, load_config, parse_config

from helpers import model_dict

DESIGN_EXAMPLE = """\
upstream_base_url: https://generativelanguage.googleapis.com/v1beta/openai/
request_deadline_s: 180
max_attempts: 3
models:   # limits: copy your own project's values from AI Studio
  lite-3-5: { id: gemini-3.5-flash-lite, rpm: 15, tpm_input: 250000, rpd: 500,
              input_limit: 1048576, output_limit: 65536 }   # limits from models.list
  lite-3-1: { id: gemini-3.1-flash-lite, rpm: 15, tpm_input: 250000, rpd: 500,
              input_limit: 1048576, output_limit: 65536 }
routes:
  gemini-fast: [lite-3-5]   # add lite-3-1 once its tool call passes the probe
handoffs: []   # must stay empty in v1 (§5.1)
"""


def base(**over):
    d = {"models": {"a": model_dict("model-a"), "b": model_dict("model-b")},
         "routes": {"fast": ["a", "b"]}}
    d.update(over)
    return d


def test_design_example_loads(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(DESIGN_EXAMPLE)
    cfg = load_config(p)
    assert [m.id for m in cfg.route_models("gemini-fast")] == ["gemini-3.5-flash-lite"]
    assert cfg.models["lite-3-1"].key == "lite-3-1"
    assert cfg.max_attempts == 3 and cfg.request_deadline_s == 180


@pytest.mark.parametrize("data, needle", [
    (base(routes={"fast": ["a", "nope"]}), "unknown model"),
    (base(handoffs=[{"from": "a", "to": "b", "signature": "keep"}]), "handoffs must be empty"),
    (base(models={"a": model_dict("same"), "b": model_dict("same")}), "share model ID"),
    (base(routes={"model-a": ["a"]}), "same name as a model ID"),
    (base(routes={"fast": []}), "has no models"),
    (base(routes={"fast": ["a", "a"]}), "twice"),
    (base(routes={}), "no routes"),
    (base(models={}), "no models"),
    (base(max_attemps=3), "max_attemps"),  # typo is caught
    (base(models={"a": {**model_dict("model-a"), "rpm": 0}, "b": model_dict("model-b")}), "rpm"),
    (base(models={"a": {**model_dict("model-a"), "key": "x"}, "b": model_dict("model-b")}), "key"),
])
def test_invalid_config_is_a_startup_error(data, needle):
    with pytest.raises(ConfigError, match=needle):
        parse_config(data)


def test_missing_file_and_bad_yaml(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("models: [unclosed")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(bad)
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(empty)


def test_config_path_is_a_folder_explains_docker_fix(tmp_path):
    # Docker creates an empty folder when the bind-mounted config.yaml is missing (DESIGN §21.2).
    folder = tmp_path / "config.yaml"
    folder.mkdir()
    with pytest.raises(ConfigError, match=r"is a folder.*rmdir config\.yaml"):
        load_config(folder)
