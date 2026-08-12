"""Скрин-доказательство подачи уходит в чат Telegram.

Иван: «после отправки в чат бота кидался пруф скрина, где видно отправку».
Проверяем ровно то, что может сломаться молча:
  - страница целиком (бывает в десятки тысяч пикселей) превращается в компактный
    JPEG, который Telegram примет: ширина ограничена, высота обрезана сверху —
    там и висит подтверждение сайта;
  - подача не падает, если скрин почему-то не подготовился;
  - успешная подача действительно зовёт отправку пруфа.
"""
import base64
import io
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import apply


def _fake_screenshot(tmp_path, size=(1440, 9000)):
    from PIL import Image
    path = tmp_path / "shot.png"
    Image.new("RGB", size, (18, 20, 21)).save(path)
    return path


def test_full_page_screenshot_becomes_small_jpeg(tmp_path):
    from PIL import Image
    b64 = apply._proof_photo_b64(_fake_screenshot(tmp_path))
    assert b64, "скрин не подготовился"
    raw = base64.b64decode(b64)
    assert len(raw) < apply.PROOF_MAX_BYTES
    with Image.open(io.BytesIO(raw)) as im:
        assert im.format == "JPEG"
        assert im.width == apply.PROOF_MAX_W
        assert im.height <= apply.PROOF_MAX_H, "очень длинную картинку Telegram не примет"


def test_small_screenshot_keeps_its_size(tmp_path):
    from PIL import Image
    b64 = apply._proof_photo_b64(_fake_screenshot(tmp_path, size=(600, 800)))
    with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
        assert im.size == (600, 800)


def test_broken_file_does_not_raise(tmp_path):
    bad = tmp_path / "not-an-image.png"
    bad.write_text("это не картинка", encoding="utf-8")
    assert apply._proof_photo_b64(bad) == ""


def test_proof_is_sent_with_honest_caption(tmp_path):
    job = mock.Mock(id="j1", title="Kasseassistent", brand="Netto", city="København")
    path = _fake_screenshot(tmp_path, size=(800, 1200))
    with mock.patch.dict(sys.modules, {"cloud_auth": mock.Mock()}):
        sys.modules["cloud_auth"].report_apply_proof = mock.Mock(return_value=True)
        apply._cloud_proof(job, path, "receipt")
        sent = sys.modules["cloud_auth"].report_apply_proof
        assert sent.call_count == 1
        job_id, b64, caption = sent.call_args.args[0], sent.call_args.args[1], sent.call_args.args[2]
        assert job_id == "j1" and b64
        assert "Заявка отправлена" in caption and "Kasseassistent" in caption

        # без квитанции подпись обязана быть честной, а не «отправлено»
        sent.reset_mock()
        apply._cloud_proof(job, path, "indirect")
        assert "без квитанции" in sent.call_args.args[2].lower()


def test_missing_proof_is_silent():
    """Скрина нет — просто ничего не шлём, подача от этого не страдает."""
    with mock.patch.dict(sys.modules, {"cloud_auth": mock.Mock()}):
        sys.modules["cloud_auth"].report_apply_proof = mock.Mock()
        apply._cloud_proof(mock.Mock(id="j2"), None, "receipt")
        sys.modules["cloud_auth"].report_apply_proof.assert_not_called()


def test_prepared_run_has_its_own_caption(tmp_path):
    """Прогон «Подготовить» тоже шлёт скрин — но подпись не должна врать «отправлено»."""
    job = mock.Mock(id="j3", title="Ungarbejder", brand="Lidl", city="Brønshøj")
    path = _fake_screenshot(tmp_path, size=(700, 900))
    with mock.patch.dict(sys.modules, {"cloud_auth": mock.Mock()}):
        sys.modules["cloud_auth"].report_apply_proof = mock.Mock(return_value=True)
        apply._cloud_proof(job, path, "prepared")
        caption = sys.modules["cloud_auth"].report_apply_proof.call_args.args[2]
    assert "подготовлена" in caption.lower()
    assert "НЕ нажата" in caption
    assert "отправлена" not in caption.lower().replace("отправка не нажата", "")


def test_prepared_screenshots_go_to_separate_folder(tmp_path, monkeypatch):
    """Скрин прогона не должен попасть в журнал подач как доказательство отправки."""
    import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    page = mock.Mock()
    page.screenshot = mock.Mock()
    job = mock.Mock(id="j4", requisition_id="req4")
    path = apply._save_proof(page, job, subdir="prepared")
    assert path is not None and path.parent.name == "prepared"
    assert (tmp_path / "logs" / "prepared").is_dir()
    assert not (tmp_path / "logs" / "applied").exists()


def test_failed_screenshots_go_to_diagnostics_not_applied(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    page = mock.Mock()
    job = mock.Mock(id="j-failed", requisition_id="req-failed")
    path = apply._save_proof(page, job, subdir="failed")
    assert path is not None and path.parent.name == "failed"
    assert not (tmp_path / "logs" / "applied").exists()


def test_successful_dry_run_is_reported_as_prepared():
    """Скрин 29.07: анкета была заполнена, а телефон писал «Прогон не удался».
    Причина — успех прогона считали по process_job, который возвращает
    «отправлено ли» и в прогоне ВСЕГДА False."""
    state, msg = apply._prepare_report("")
    assert state == "prepared"
    assert "НЕ нажата" in msg

    state, msg = apply._prepare_report("Timeout 30000ms")
    assert state == "prepare_failed"
    assert "Timeout 30000ms" in msg, "причину сбоя надо показывать, а не прятать"


def test_run_batch_reports_prepared_without_submitting():
    """Страховка от возврата бага: в ветке прогона нельзя опираться на ok."""
    import inspect
    src = inspect.getsource(apply.run_batch)
    assert "_prepare_report(job_error)" in src
    assert '"prepared" if ok' not in src, "успех прогона снова считается по отправке"
