"""PDF 上傳檢查（林）：副檔名、檔頭、大小、頁數，以及登入、CSRF、擁有者。"""
import io

import pytest

from app import create_app
from tests.conftest import As


# ---------------- 測試用 PDF ----------------
def make_pdf(pages: list[str]) -> bytes:
    """手寫一份最小的合法 PDF，每頁一行英文文字（Helvetica 不含中文字型）。"""
    n = len(pages)
    font_id = 3 + 2 * n
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + b" ".join(f"{3 + 2 * i} 0 R".encode() for i in range(n))
        + b"] /Count " + str(n).encode() + b" >>",
    ]
    for i, text in enumerate(pages):
        page_id, content_id = 3 + 2 * i, 4 + 2 * i
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                    f"/Contents {content_id} 0 R >>".encode())
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


GOOD = make_pdf(["Week 3 supervised learning", "Overfitting and regularization"])


def upload(user, data, filename="notes.pdf", title=None, url="/api/materials"):
    form = {"file": (io.BytesIO(data), filename)}
    if title is not None:
        form["title"] = title
    if url.startswith("/api/"):
        return user.api_post(url, data=form, content_type="multipart/form-data")
    return user.post(url, data=form, content_type="multipart/form-data")


# ---------------- 登入與 CSRF ----------------
def test_api_requires_login(client):
    r = client.post("/api/materials", data={"file": (io.BytesIO(GOOD), "a.pdf")},
                    content_type="multipart/form-data")
    # 沒登入時 session 裡沒有 csrf，CSRF 檢查先擋（400）；兩者都不會進到上傳
    assert r.status_code in (400, 401)


def test_api_requires_login_even_with_csrf_disabled():
    app = create_app({"TESTING": True, "SECRET_KEY": "t", "CSRF_DISABLED": True})
    r = app.test_client().post("/api/materials", data={"file": (io.BytesIO(GOOD), "a.pdf")},
                               content_type="multipart/form-data")
    assert r.status_code == 401


def test_form_redirects_to_login(app):
    app.config["CSRF_DISABLED"] = True
    r = app.test_client().post("/materials/upload", data={"file": (io.BytesIO(GOOD), "a.pdf")},
                               content_type="multipart/form-data")
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_csrf_required(login):
    alice = login("alice")
    r = alice.c.post("/api/materials", data={"file": (io.BytesIO(GOOD), "a.pdf")},
                     content_type="multipart/form-data")
    assert r.status_code == 400


# ---------------- 成功 ----------------
def test_upload_ok_and_owned_by_uploader(app, login):
    alice = login("alice")
    r = upload(alice, GOOD, title="AI 第 3 週")
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["owner_id"] == "alice" and body["pages"] == 2

    m = app.extensions["store"].materials[body["id"]]
    assert "supervised learning" in m.pages[0]
    assert "Overfitting" in m.pages[1]
    assert m.title == "AI 第 3 週" and m.filename == "notes.pdf"

    # 上傳後自己看得到，別人看不到（接上儀珊的 ABAC）
    assert alice.get(f"/api/materials/{body['id']}").status_code == 200
    assert login("bob").get(f"/api/materials/{body['id']}").status_code == 403


def test_title_defaults_to_filename(app, login):
    r = upload(login("alice"), GOOD, filename="AI_week5.pdf")
    assert r.get_json()["title"] == "AI_week5"


def test_uppercase_extension_ok(login):
    assert upload(login("alice"), GOOD, filename="NOTES.PDF").status_code == 201


def test_path_in_filename_is_stripped(app, login):
    r = upload(login("alice"), GOOD, filename="../../etc/evil.pdf")
    assert r.status_code == 201
    assert app.extensions["store"].materials[r.get_json()["id"]].filename == "evil.pdf"


def test_form_upload_redirects_to_material(login):
    alice = login("alice")
    r = upload(alice, GOOD, url="/materials/upload")
    assert r.status_code == 302 and "/materials/" in r.headers["Location"]
    page = alice.get(r.headers["Location"]).get_data(as_text=True)
    assert "supervised learning" in page and "已上傳" in page


def test_title_is_escaped_on_dashboard(login):
    alice = login("alice")
    upload(alice, GOOD, title="<script>alert(1)</script>")
    html = alice.get("/").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_dashboard_shows_form(login):
    html = login("alice").get("/").get_data(as_text=True)
    assert 'enctype="multipart/form-data"' in html and 'name="csrf"' in html


# ---------------- 副檔名 ----------------
@pytest.mark.parametrize("name", ["notes.txt", "notes.pdf.exe", "notes", "notes.pdfx"])
def test_bad_extension_rejected(login, name):
    r = upload(login("alice"), GOOD, filename=name)
    assert r.status_code == 415 and r.get_json()["error"] == "bad_extension"


def test_missing_file_rejected(login):
    alice = login("alice")
    r = alice.api_post("/api/materials", data={}, content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["error"] == "no_file"


# ---------------- 檔頭 ----------------
@pytest.mark.parametrize("data", [
    b"MZ\x90\x00 this is a windows exe",          # 改副檔名的執行檔
    b"\x89PNG\r\n\x1a\n fake",                   # 改副檔名的圖片
    b"<html><script>alert(1)</script></html>",   # 改副檔名的 HTML
    b" %PDF-1.4 leading space",                  # 檔頭不在最前面
])
def test_bad_magic_rejected(login, data):
    r = upload(login("alice"), data)
    assert r.status_code == 415 and r.get_json()["error"] == "bad_magic"


def test_empty_file_rejected(login):
    r = upload(login("alice"), b"")
    assert r.status_code == 400 and r.get_json()["error"] == "empty"


def test_corrupted_pdf_rejected(login):
    r = upload(login("alice"), b"%PDF-1.4\nthis is not really a pdf\n%%EOF")
    assert r.status_code == 400 and r.get_json()["error"] == "unreadable"


# ---------------- 大小 ----------------
@pytest.fixture
def small_app():
    return create_app({"TESTING": True, "SECRET_KEY": "t",
                       "UPLOAD_MAX_BYTES": len(GOOD) + 10, "UPLOAD_MAX_PAGES": 3})


def test_file_at_limit_ok(small_app):
    assert upload(As(small_app.test_client(), "alice"), GOOD).status_code == 201


def test_file_over_limit_rejected(small_app):
    big = GOOD + b"\n%" + b"x" * 100       # 檔尾加註解，還是合法 PDF，只是變大
    r = upload(As(small_app.test_client(), "alice"), big)
    assert r.status_code == 413 and r.get_json()["error"] == "too_large"


def test_request_over_max_content_length_rejected(small_app):
    """整個請求超過 MAX_CONTENT_LENGTH：Flask 讀表單前就擋掉。"""
    huge = b"%PDF-1.4\n" + b"x" * (small_app.config["MAX_CONTENT_LENGTH"] + 1)
    r = upload(As(small_app.test_client(), "alice"), huge)
    assert r.status_code == 413


def test_request_too_large_on_form_shows_message(small_app):
    alice = As(small_app.test_client(), "alice")
    huge = b"%PDF-1.4\n" + b"x" * (small_app.config["MAX_CONTENT_LENGTH"] + 1)
    r = upload(alice, huge, url="/materials/upload")
    assert r.status_code == 302
    assert "上限" in alice.get("/").get_data(as_text=True)


# ---------------- 頁數與內容 ----------------
def test_pages_at_limit_ok(small_app):
    pdf = make_pdf(["p1", "p2", "p3"])
    small_app.config["UPLOAD_MAX_BYTES"] = 10_000
    assert upload(As(small_app.test_client(), "alice"), pdf).status_code == 201


def test_too_many_pages_rejected(small_app):
    pdf = make_pdf(["p1", "p2", "p3", "p4"])
    small_app.config["UPLOAD_MAX_BYTES"] = 10_000
    r = upload(As(small_app.test_client(), "alice"), pdf)
    assert r.status_code == 400 and r.get_json()["error"] == "too_many_pages"


def test_no_text_rejected(login):
    r = upload(login("alice"), make_pdf(["", ""]))
    assert r.status_code == 400 and r.get_json()["error"] == "no_text"


def test_encrypted_pdf_rejected(login):
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter()
    for p in PdfReader(io.BytesIO(GOOD)).pages:
        w.add_page(p)
    w.encrypt(user_password="secret", algorithm="RC4-128")
    buf = io.BytesIO()
    w.write(buf)
    r = upload(login("alice"), buf.getvalue())
    assert r.status_code == 400 and r.get_json()["error"] == "encrypted"


def test_text_per_page_is_capped(app, login):
    app.config["UPLOAD_MAX_CHARS_PER_PAGE"] = 10
    r = upload(login("alice"), GOOD)
    m = app.extensions["store"].materials[r.get_json()["id"]]
    assert all(len(p) <= 10 for p in m.pages)


def test_rejected_upload_is_not_stored(app, login):
    before = len(app.extensions["store"].materials)
    upload(login("alice"), b"MZ not a pdf")
    assert len(app.extensions["store"].materials) == before
