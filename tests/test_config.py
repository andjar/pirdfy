import pytest
import yaml
from pydantic import ValidationError

from pirdfy.config import MASK, ConfigStore, Settings, format_template


def test_defaults_without_file(tmp_path):
    store = ConfigStore(tmp_path / "missing.yaml")
    assert store.settings == Settings()


def test_partial_override_file(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("web:\n  port: 9000\ncapture:\n  motion:\n    enabled: false\n")
    s = ConfigStore(f).settings
    assert s.web.port == 9000
    assert s.capture.motion.enabled is False
    assert s.capture.interval == Settings().capture.interval


def test_empty_file_is_ok(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("# only comments\n{}\n")
    assert ConfigStore(f).settings == Settings()


def test_unknown_key_rejected(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("web:\n  prot: 1\n")
    with pytest.raises(ValidationError):
        ConfigStore(f)


def test_update_persists_only_overrides(tmp_path):
    f = tmp_path / "c.yaml"
    store = ConfigStore(f)
    store.update({"video": {"clip_seconds": 30}})
    saved = yaml.safe_load(f.read_text())
    assert saved == {"video": {"clip_seconds": 30}}
    assert ConfigStore(f).settings.video.clip_seconds == 30


def test_update_invalid_keeps_old(tmp_path):
    store = ConfigStore(tmp_path / "c.yaml")
    with pytest.raises(ValidationError):
        store.update({"video": {"clip_seconds": 30, "max_clip_seconds": 10}})
    assert store.settings.video.clip_seconds == 20
    assert not (tmp_path / "c.yaml").exists()


def test_camera_patch_merges_by_id(tmp_path):
    store = ConfigStore(tmp_path / "c.yaml")
    store.update({"cameras": [{"id": 0, "name": "Left"}, {"id": 1, "name": "Right"}]})
    names = [c.name for c in store.settings.cameras]
    assert names == ["Left", "Right"]
    assert store.settings.cameras[0].resolution == (1920, 1080)
    with pytest.raises(ValidationError):
        store.update({"cameras": [{"id": 0}, {"id": 0}]})


def test_secret_urls_masked_and_preserved(tmp_path):
    store = ConfigStore(tmp_path / "c.yaml")
    store.update({"notifications": {"urls": ["tgram://secret-token/123"]}})
    public = store.to_public_dict()
    assert public["notifications"]["urls"] == [f"tgram://{MASK}"]
    # Sending the masked value back keeps the secret; new entries are added.
    store.update({"notifications": {"urls": public["notifications"]["urls"] + ["ntfy://ntfy.sh/x"]}})
    assert store.settings.notifications.urls == ["tgram://secret-token/123", "ntfy://ntfy.sh/x"]


def test_listeners_notified(tmp_path):
    store = ConfigStore(tmp_path / "c.yaml")
    seen = []
    store.on_change(lambda old, new: seen.append((old.web.port, new.web.port)))
    store.update({"web": {"port": 8181}})
    assert seen == [(8080, 8181)]


@pytest.mark.parametrize("bad", ["{nope}", "{count", "{confidence:.0%"])
def test_template_validation(bad, tmp_path):
    store = ConfigStore(tmp_path / "c.yaml")
    with pytest.raises(ValidationError):
        store.update({"notifications": {"body": bad}})


def test_unknown_class_rejected(tmp_path):
    with pytest.raises(ValidationError):
        ConfigStore(tmp_path / "c.yaml").update({"detection": {"classes": ["squirrel"]}})


def test_format_template_safe():
    assert format_template("{count} at {time} {other}", count=2, time="12:00") == "2 at 12:00 {other}"
