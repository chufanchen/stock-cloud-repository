import io
import json
import tarfile
from pathlib import Path

import pytest

from ashare_cloud import pages
from ashare_cloud.common import file_hash, read_json, write_json
from ashare_cloud.storage import Store
from test_storage import FakeS3


def seed(root, day="2026-10-19", equity=100013.62, pnl=0, status="ok"):
    write_json(root / f"reports/{day}.json", {"facts": {"date": day,
        "settlement": {"date": day, "equity": equity, "cash": equity, "market_value": 0, "day_pnl": pnl, "realized_pnl": 0,
                       "buys": [], "sells": [], "rejects": [{"code": "600001", "name": "演示股票", "reason": "缺少竞价数据"}],
                       "positions": [{"code": "600001", "name": "测试", "qty": 100, "mark": 10, "buy_price": 9,
                                      "config": {"PRIVATE_CONFIG": "DO_NOT_PUBLISH"}, "plan_hash": "PRIVATE_PLAN_HASH"}],
                       "events": [], "controls": {"PRIVATE_CONTROL": "not for website"}},
        "next_plan": {"trade_date": "2026-10-20", "candidate_count": 1, "buy_paused": False, "picks": [
            {"code": "600001", "name": "测试股票", "pos": .08, "prev_close": 10}], "hash": "PRIVATE_HASH"},
        "market_coverage": .99, "limitations": ["模拟交易测试资料"], "SECRET_FACT": "PRIVATE_PROMPT"},
        "ai": {"status": status, "content": "# 今日复盘\n<script>alert('xss')</script>\n- 未经验证的观点", "error": "PRIVATE_ERROR", "model": "PRIVATE_MODEL"}})
    write_json(root / f"meeting/days/{day}.json", {"date": day, "status": "ok", "settings": {"roles": [{"id": "seat", "name": "测试席位"}]},
        "input": {"previous_memory": "PRIVATE_MEMORY", "facts": "PRIVATE_PROMPT"}, "stages": {
            "seat": {"status": "ok", "data": {"summary": "测试观点", "observations": ["测试观察"], "questions": [], "memory_notes": ["PRIVATE_ROLE_MEMORY"]}},
            "moderator": {"status": "ok", "data": {"summary": "测试汇总", "agreements": [], "disagreements": []}}}})
    write_json(root / "research/proposals.json", {"proposals": [{"id": "rp-1", "title": "测试假设", "hypothesis": "待验证",
        "status": "approved", "created_date": day, "updated_date": day,
        "observations": [{"date": day, "evidence": "测试证据", "test_plan": "测试计划", "rejection_criterion": "样本外无效", "data_needed": "历史数据", "source": "PRIVATE_SOURCE"}]}]})


def test_website_escapes_ai_and_exports_only_presentation_fields(tmp_path):
    source, out = tmp_path / "state", tmp_path / "site"
    seed(source)
    before = file_hash(source / "reports/2026-10-19.json")
    pages.build_site(source, out)
    exported = "\n".join(p.read_text() for p in out.rglob("*") if p.is_file())
    assert "PRIVATE_" not in exported and "DO_NOT_PUBLISH" not in exported
    assert "<script>alert" not in exported and "&lt;script&gt;" in exported
    assert not (out / "account.json").exists() and not (out / "reports").exists()
    assert "100,013.62" in (out / "index.html").read_text()
    assert "待验证" in (out / "research.html").read_text() and "approved" not in exported
    assert before == file_hash(source / "reports/2026-10-19.json")
    assert "../assets/site.css" in (out / "days/2026-10-19.html").read_text()


def test_failed_ai_and_pending_newer_report_are_explicit(tmp_path):
    source, out = tmp_path / "state", tmp_path / "site"
    seed(source, status="failed")
    pages.build_site(source, out, {"last_settled": "2026-10-20"})
    content = (out / "index.html").read_text()
    assert "复盘待重试" in content and "较新的模拟结算" in content
    assert "2026-10-19" in content and "PRIVATE_ERROR" not in content


def test_empty_site_is_honest_and_never_creates_fake_values(tmp_path):
    pages.build_site(tmp_path / "empty", tmp_path / "out")
    content = (tmp_path / "out/index.html").read_text()
    assert "等待第一份收盘复盘" in content and "100,000" not in content
    assert read_json(tmp_path / "out/publication.json")["report_count"] == 0


def test_no_raw_or_existing_files_can_be_accidentally_published(tmp_path):
    source = tmp_path / "state"
    seed(source)
    with pytest.raises(ValueError, match="分开"):
        pages.build_site(source, source / "site")
    target = tmp_path / "out"
    target.mkdir()
    (target / "secret.env").write_text("private")
    with pytest.raises(ValueError, match="必须为空"):
        pages.build_site(source, target)


def test_pull_source_is_readonly_and_does_not_download_database(tmp_path):
    root = tmp_path / "state"
    seed(root)
    (root / "market.db").write_bytes(b"database fixture")
    write_json(root / "account.json", {"last_settled": "2026-10-19", "private": True})
    write_json(root / "meeting/memory/2026-10-19.json", {"private": True})
    s3 = FakeS3()
    store = Store(s3, "bucket", "prefix")
    store.push(root, bootstrap=True)
    generation = s3.generation
    files = dict(s3.files)
    original = s3.download_file
    def download(bucket, key, filename):
        assert "/databases/" not in key
        original(bucket, key, filename)
    s3.download_file = download
    target = tmp_path / "publication-source"
    source = pages.pull_source(target, Store(s3, "bucket", "prefix"))
    assert source == {"last_settled": "2026-10-19", "initialized": True}
    assert s3.files == files and s3.generation == generation
    assert (target / "reports/2026-10-19.json").exists()
    assert not (target / "account.json").exists() and not (target / "market.db").exists()
    assert not (target / "meeting/memory").exists()


def test_missing_r2_state_can_publish_empty_landing(tmp_path):
    assert pages.pull_source(tmp_path / "source", Store(FakeS3(), "bucket", "prefix"))["initialized"] is False


def test_bad_archive_is_rejected_before_publication(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("../escape.json")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    s3 = FakeS3()
    s3.files["prefix/states/test.tar.gz"] = archive.read_bytes()
    s3.files["prefix/HEAD.json"] = json.dumps({"state_key": "prefix/states/test.tar.gz", "state_sha256": file_hash(archive)}).encode()
    with pytest.raises(ValueError, match="不安全"):
        pages.pull_source(tmp_path / "source", Store(s3, "bucket", "prefix"))


def test_date_mismatch_refuses_build(tmp_path):
    seed(tmp_path / "source")
    p = tmp_path / "source/reports/2026-10-19.json"
    d = read_json(p)
    d["facts"]["date"] = "2026-10-20"
    write_json(p, d)
    with pytest.raises(ValueError, match="日期不一致"):
        pages.build_site(tmp_path / "source", tmp_path / "out")


def test_all_generated_internal_links_work_under_project_subpath(tmp_path):
    from html.parser import HTMLParser
    source, out = tmp_path / "source", tmp_path / "out"
    seed(source)
    seed(source, "2026-10-20", 100123.62, 110)
    pages.build_site(source, out)
    class Links(HTMLParser):
        def handle_starttag(self, tag, attrs):
            for key, val in attrs:
                if key in ("href", "src") and val and not val.startswith("#"):
                    assert not val.startswith(("/", "http"))
                    assert (self.parent / val).exists(), val
    for p in out.rglob("*.html"):
        parser = Links()
        parser.parent = p.parent
        parser.feed(p.read_text())
    assert "polyline" in (out / "index.html").read_text()
