import re
from datetime import datetime, timedelta

from conftest import jpeg, login


def _up(tech, taken, color=(60, 70, 80), exif_date=None):
    r = tech.post("/api/media", data={"taken_at": taken}, files=[("files", ("wm.jpg", jpeg(color, exif_date), "image/jpeg"))])
    assert r.status_code == 200, r.text
    return r.json()[0]["taken_at"]


def test_watermark_camera_taken_at(env):
    tech = login("tech1")
    ts = (datetime.now() - timedelta(minutes=3)).isoformat(timespec="minutes")
    assert _up(tech, ts) == ts                                            # 水印相机重绘图无 EXIF：采信前端拍摄时间
    assert _up(tech, "", (61, 71, 81)) is None                            # 没带时间就不造时间
    assert _up(tech, "胡写", (62, 72, 82)) is None                        # 非法值忽略
    future = (datetime.now() + timedelta(days=2)).isoformat(timespec="minutes")
    assert _up(tech, future, (63, 73, 83)) is None                        # 未来时间不采信
    assert _up(tech, ts, (64, 74, 84), exif_date="2020:01:02 03:04:05") == "2020-01-02T03:04"  # 有 EXIF 以 EXIF 为准


def _geo_form(lat, lon, acc="12", place=""):
    return {"geo_lat": lat, "geo_lon": lon, "geo_acc": acc, "place": place}


def test_phone_location_on_photos_notes_and_feed(env):
    from test_roles import _team
    _team(env)
    tech = login("tech1")
    # 照片：坐标、精度、地点入库并随照片返回；非法坐标丢弃（不补零），仅地点文字保留
    ok = tech.post("/api/media", data=_geo_form("30.5928", "114.3055", "9.6", "3号墩承台"), files=[("files", ("a.jpg", jpeg((10, 20, 30)), "image/jpeg"))]).json()[0]
    assert ok["geo"] == {"lat": 30.5928, "lon": 114.3055, "acc": 10, "place": "3号墩承台"}
    bad = tech.post("/api/media", data=_geo_form("999", "114", "5", "K1+200"), files=[("files", ("b.jpg", jpeg((11, 21, 31)), "image/jpeg"))]).json()[0]
    assert bad["geo"] == {"lat": None, "lon": None, "acc": None, "place": "K1+200"}
    none = tech.post("/api/media", data=_geo_form("", "", ""), files=[("files", ("c.jpg", jpeg((12, 22, 32)), "image/jpeg"))]).json()[0]
    assert none["geo"] is None

    # 随手记：自带定位；纯文字无定位如实为空；只有照片带定位时沿用照片的
    n1 = tech.post("/api/notes", json={"text": "承台钢筋绑扎", "lat": 30.6, "lon": 114.3, "acc": 20}).json()
    assert n1["geo"]["lat"] == 30.6 and n1["geo"]["acc"] == 20
    n2 = tech.post("/api/notes", json={"text": "没有定位的记录"}).json()
    assert n2["geo"] is None
    n3 = tech.post("/api/notes", json={"text": "", "media_ids": [ok["id"]], "source": "photo"}).json()
    assert n3["geo"]["lat"] == 30.5928

    # 动态流：随手记/随手拍/整改带定位，周确认不要求定位
    snap = tech.post("/api/media", data={"purpose": "safety", **_geo_form("30.58", "114.31", "8", "基坑边")},
                     files=[("files", ("d.jpg", jpeg((200, 60, 60)), "image/jpeg"))]).json()[0]
    posts = {p["key"]: p for p in tech.get("/api/feed").json()["posts"]}
    assert posts[f"note:{n1['id']}"]["geo"]["lon"] == 114.3 and posts[f"note:{n1['id']}"]["geo_slot"]
    assert posts[f"note:{n2['id']}"]["geo"] is None and posts[f"note:{n2['id']}"]["geo_slot"]
    sp = next(p for p in posts.values() if p["kind"] == "snap")
    assert sp["geo"]["place"] == "基坑边" and sp["geo"]["lat"] == 30.58


def test_watermark_labels_all_chinese():
    """U-40、D4：水印标签统一中文（不再“Work Area / Work Content”与“拍摄时间”混排），横拍竖拍同一模板，三行取值左对齐。"""
    from test_capture_front import STATIC, cam
    out = cam("""
      const draw = (w, h, info) => { const c = document.createElement('canvas'), g = c.getContext('2d'); wmDraw(g, w, h, info); return { t: g.texts, x: g.xs }; };
      const info = { when: new Date(2026, 9, 7, 9, 5), place: 'CH10+950 3#墩', work: '承台钢筋绑扎', proj: '测试项目' };
      out.land = draw(2000, 1125, info); out.port = draw(1080, 1920, info);
    """)
    for k in ("land", "port"):
        texts, xs = out[k]["t"], out[k]["x"]
        labels = [t for t in texts if t.endswith("：") or t.endswith(":")]
        assert labels == ["工作地点：", "工作内容：", "拍摄时间："], texts
        assert "2026.10.07 09:05" in texts
        x = dict(zip(texts, xs))
        assert x["CH10+950 3#墩"] == x["承台钢筋绑扎"] == x["2026.10.07 09:05"], (k, x)
    src = (STATIC / "camera.js").read_text(encoding="utf-8")
    assert not re.search(r"Work (Area|Content)", src)
