from flask import Flask, flash, jsonify, request, render_template, session, redirect, url_for
from urllib.parse import urlparse, urljoin, urlencode
from functools import wraps
import json
import math
import os
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# app.py はプロジェクト直下に置く。
# 実体（templates / static / data）は bousai_app/ 配下にあるので、そこを参照する。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, 'bousai_app')

app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, 'templates'),
    static_folder=os.path.join(APP_DIR, 'static'),
)
app.secret_key = 'your-secret-key-here'

# 管理者認証情報
ADMIN_CREDENTIALS = {
    'admin': '123'
}

# ────────────────────────────────
# 気象警報・注意報設定
PREFECTURE_CODE = "020000"  # 青森県
AREA_NAME = "青森市"

# 青森市の市区町村コード
AREA_CODE = "0220100"
GEOCODING_URL = "https://nominatim.openstreetmap.org/search"
GSI_GEOCODING_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"
GEOCODING_CACHE = {}
GEOCODING_REGION_CACHE = {}
GEOCODING_LOCK = threading.Lock()
LAST_GEOCODING_REQUEST = 0.0

WARNING_URL = (
    f"https://www.jma.go.jp/bosai/warning/data/r8/{PREFECTURE_CODE}.json"
)

JST = timezone(timedelta(hours=9))

# 警報・注意報のコード一覧
WARNING_CODES = {
    "00": "解除",
    "02": "暴風雪警報",
    "03": "レベル3大雨警報",
    "04": "洪水警報",
    "05": "暴風警報",
    "06": "大雪警報",
    "07": "波浪警報",
    "08": "レベル3高潮警報",
    "09": "レベル3土砂災害警報",
    "10": "レベル2大雨注意報",
    "12": "大雪注意報",
    "13": "風雪注意報",
    "14": "雷注意報",
    "15": "強風注意報",
    "16": "波浪注意報",
    "17": "融雪注意報",
    "18": "洪水注意報",
    "19": "レベル2高潮注意報",
    "20": "濃霧注意報",
    "21": "乾燥注意報",
    "22": "なだれ注意報",
    "23": "低温注意報",
    "24": "霜注意報",
    "25": "着氷注意報",
    "26": "着雪注意報",
    "27": "その他の注意報",
    "29": "レベル2土砂災害注意報",
    "32": "暴風雪特別警報",
    "33": "レベル5大雨特別警報",
    "35": "暴風特別警報",
    "36": "大雪特別警報",
    "37": "波浪特別警報",
    "38": "レベル5高潮特別警報",
    "39": "レベル5土砂災害特別警報",
    "43": "レベル4大雨危険警報",
    "48": "レベル4高潮危険警報",
    "49": "レベル4土砂災害危険警報"
}

DISASTER_CATEGORIES = {
    "weather": "気象情報",
    "evacuation": "避難情報",
    "road": "道路情報",
    "operations": "対応状況"
}

WARNING_ICONS = {
    "02": "wind", "05": "wind", "13": "wind", "15": "wind",
    "32": "wind", "35": "wind", "03": "rain", "09": "rain",
    "10": "rain", "29": "rain", "33": "rain", "39": "rain",
    "43": "rain", "49": "rain", "04": "flood", "18": "flood",
    "06": "snow", "12": "snow", "22": "snow", "26": "snow",
    "36": "snow", "14": "lightning", "07": "wave", "16": "wave",
    "08": "wave", "19": "wave", "37": "wave", "38": "wave",
    "48": "wave"
}

# ────────────────────────────────
# サンプルデータの読み込み
DATA_FILE = os.path.join(APP_DIR, 'data', 'shelters.json')
INSTRUCTIONS_FILE = os.path.join(APP_DIR, 'data', 'instructions.json')

def load_json(path, default):
    """JSONファイルを読み込む（存在しない・壊れている場合は default を返す）"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

shelters = load_json(DATA_FILE, [])
instructions = load_json(INSTRUCTIONS_FILE, [])

def save_instructions():
    """指示ボードのデータをファイルに保存する"""
    try:
        with open(INSTRUCTIONS_FILE, 'w', encoding='utf-8') as f:
            json.dump(instructions, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
# ────────────────────────────────

# ────────────────────────────────
# 認証関連の設定とヘルパー関数
def is_safe_url(target):
    """リダイレクト先URLが安全かどうかチェック"""
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

def login_required(f):
    """認証が必要なページに付けるデコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            # 現在のURLをnextパラメータとしてログイン画面にリダイレクト
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def get_japan_time():
    """日本時間（JST）の現在時刻を取得する"""
    return datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")


def format_report_time(iso_str):
    """気象庁の発表時刻（ISO形式）をJSTの表示用文字列に変換する"""
    if not iso_str:
        return "不明"
    try:
        parsed = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(JST)
        return parsed.strftime("%Y年%m月%d日 %H:%M")
    except ValueError:
        return iso_str


def filter_shelters(district=None):
    """district 指定があれば一致する避難所のみ、なければ全件を返す"""
    return [s for s in shelters if not district or s.get('district') == district]


def search_shelter_locations(query, region):
    """国土地理院を優先し、施設名・住所の候補地点を検索する"""
    global LAST_GEOCODING_REQUEST
    normalized_query = " ".join(f"{region} {query}".split())
    cache_key = normalized_query.casefold()
    if cache_key in GEOCODING_CACHE:
        return GEOCODING_CACHE[cache_key]

    gsi_parameters = urlencode({"q": query})
    with GEOCODING_LOCK:
        wait_time = 1.1 - (time.monotonic() - LAST_GEOCODING_REQUEST)
        if wait_time > 0:
            time.sleep(wait_time)

        gsi_request = urllib.request.Request(
            f"{GSI_GEOCODING_URL}?{gsi_parameters}",
            headers={"User-Agent": "bousai-app/1.0 (shelter location search)"}
        )
        try:
            with urllib.request.urlopen(gsi_request, timeout=10) as response:
                gsi_results = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            gsi_results = []
        LAST_GEOCODING_REQUEST = time.monotonic()

        results = []
        if isinstance(gsi_results, list):
            for result in gsi_results:
                if not isinstance(result, dict):
                    continue
                properties = result.get("properties", {})
                coordinates = result.get("geometry", {}).get("coordinates", [])
                if not isinstance(properties, dict) or len(coordinates) < 2:
                    continue
                try:
                    longitude, latitude = float(coordinates[0]), float(coordinates[1])
                except (TypeError, ValueError):
                    continue
                if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                    continue
                results.append({
                    "name": properties.get("title", "検索候補"),
                    "address": "国土地理院地名検索",
                    "latitude": latitude,
                    "longitude": longitude
                })

        region_key = region.casefold()
        region_anchor = GEOCODING_REGION_CACHE.get(region_key)
        if region and region_anchor is None:
            wait_time = 1.1 - (time.monotonic() - LAST_GEOCODING_REQUEST)
            if wait_time > 0:
                time.sleep(wait_time)
            region_request = urllib.request.Request(
                f"{GSI_GEOCODING_URL}?{urlencode({'q': region})}",
                headers={"User-Agent": "bousai-app/1.0 (shelter location search)"}
            )
            try:
                with urllib.request.urlopen(region_request, timeout=10) as response:
                    region_results = json.loads(response.read())
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
                region_results = []
            LAST_GEOCODING_REQUEST = time.monotonic()

            if isinstance(region_results, list):
                region_anchor = next((
                    (
                        float(item["geometry"]["coordinates"][1]),
                        float(item["geometry"]["coordinates"][0])
                    )
                    for item in region_results
                    if isinstance(item, dict)
                    and isinstance(item.get("properties"), dict)
                    and region in item["properties"].get("title", "")
                    and len(item.get("geometry", {}).get("coordinates", [])) >= 2
                ), None)
                if region_anchor:
                    GEOCODING_REGION_CACHE[region_key] = region_anchor

        if region_anchor:
            for result in results:
                latitude_delta = math.radians(result["latitude"] - region_anchor[0])
                longitude_delta = math.radians(result["longitude"] - region_anchor[1])
                start_latitude = math.radians(region_anchor[0])
                end_latitude = math.radians(result["latitude"])
                distance = 6371 * 2 * math.asin(math.sqrt(
                    math.sin(latitude_delta / 2) ** 2
                    + math.cos(start_latitude) * math.cos(end_latitude)
                    * math.sin(longitude_delta / 2) ** 2
                ))
                result["distance_km"] = round(distance, 1)
                result["address"] = f"検索地域から約{distance:.1f}km / {result['address']}"
            results.sort(key=lambda result: result.get("distance_km", float("inf")))
            nearby_results = [result for result in results if result.get("distance_km", 0) <= 50]
            if nearby_results:
                results = nearby_results[:5]
            else:
                results = []

        if not results:
            wait_time = 1.1 - (time.monotonic() - LAST_GEOCODING_REQUEST)
            if wait_time > 0:
                time.sleep(wait_time)
            nominatim_parameters = urlencode({
                "q": normalized_query,
                "format": "jsonv2",
                "limit": 5,
                "addressdetails": 1,
                "countrycodes": "jp",
                "accept-language": "ja"
            })
            nominatim_request = urllib.request.Request(
                f"{GEOCODING_URL}?{nominatim_parameters}",
                headers={"User-Agent": "bousai-app/1.0 (shelter location search)"}
            )
            with urllib.request.urlopen(nominatim_request, timeout=10) as response:
                nominatim_results = json.loads(response.read())
            LAST_GEOCODING_REQUEST = time.monotonic()

            if isinstance(nominatim_results, list):
                for result in nominatim_results:
                    if not isinstance(result, dict):
                        continue
                    try:
                        latitude = float(result["lat"])
                        longitude = float(result["lon"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                        continue
                    results.append({
                        "name": result.get("name") or result.get("display_name", "検索候補").split(",")[0],
                        "address": result.get("display_name", "住所情報なし"),
                        "latitude": latitude,
                        "longitude": longitude
                    })

            if region_anchor:
                for result in results:
                    latitude_delta = math.radians(result["latitude"] - region_anchor[0])
                    longitude_delta = math.radians(result["longitude"] - region_anchor[1])
                    start_latitude = math.radians(region_anchor[0])
                    end_latitude = math.radians(result["latitude"])
                    distance = 6371 * 2 * math.asin(math.sqrt(
                        math.sin(latitude_delta / 2) ** 2
                        + math.cos(start_latitude) * math.cos(end_latitude)
                        * math.sin(longitude_delta / 2) ** 2
                    ))
                    result["distance_km"] = round(distance, 1)
                    result["address"] = f"検索地域から約{distance:.1f}km / {result['address']}"
                results.sort(key=lambda result: result.get("distance_km", float("inf")))
                nearby_results = [result for result in results if result.get("distance_km", 0) <= 50]
                if nearby_results:
                    results = nearby_results[:5]
                else:
                    results = []

    if len(GEOCODING_CACHE) >= 100:
        GEOCODING_CACHE.pop(next(iter(GEOCODING_CACHE)))
    GEOCODING_CACHE[cache_key] = results
    return results


def parse_area_warnings(warning_data):
    """気象庁の新形式JSONから対象市区町村の最新の警報・注意報を抽出する"""
    if not isinstance(warning_data, list):
        raise ValueError("気象庁の警報・注意報データが新形式の配列ではありません")

    latest_report = None
    latest_area = None
    latest_datetime = None

    for report in warning_data:
        if not isinstance(report, dict):
            continue

        warning = report.get("warning")
        if not isinstance(warning, dict):
            continue

        class20_items = warning.get("class20Items", [])
        if not isinstance(class20_items, list):
            continue

        area = next(
            (
                item for item in class20_items
                if isinstance(item, dict)
                and item.get("areaCode") == AREA_CODE
            ),
            None
        )
        if not area:
            continue

        report_datetime = report.get("reportDatetime")
        if not isinstance(report_datetime, str) or not report_datetime:
            continue
        try:
            parsed_datetime = datetime.fromisoformat(report_datetime.replace('Z', '+00:00'))
        except ValueError:
            continue

        if latest_datetime is None or parsed_datetime > latest_datetime:
            latest_datetime = parsed_datetime
            latest_report = report_datetime
            latest_area = area

    if not latest_area:
        return [], ""

    warnings = []
    kinds = latest_area.get("kinds", [])
    if not isinstance(kinds, list):
        return warnings, latest_report

    for kind in kinds:
        if not isinstance(kind, dict):
            continue

        status = kind.get("status", "")
        code = kind.get("code", "")
        if status not in ("発表", "継続") or not code:
            continue

        warnings.append({
            "name": WARNING_CODES.get(
                code,
                f"不明な警報・注意報 (コード: {code})"
            ),
            "code": code,
            "status": status
        })

    return warnings, latest_report


def get_weather_warnings():
    """対象市区町村の警報・注意報を取得する"""
    try:
        # 青森県の新形式（令和8年～）警報・注意報データを取得
        with urllib.request.urlopen(url=WARNING_URL, timeout=10) as res:
            warning_data = json.loads(res.read())

        warnings, report_datetime = parse_area_warnings(warning_data)

        return {
            "area_name": AREA_NAME,
            "warnings": warnings,
            "report_time": format_report_time(report_datetime),
            "last_fetch_time": get_japan_time()
        }

    except Exception:
        return {
            "area_name": AREA_NAME,
            "warnings": [],
            "report_time": "取得失敗",
            "last_fetch_time": get_japan_time(),
            "error": True
        }


def parse_received_datetime(value):
    """受信日時の表示形式をJSTのdatetimeに変換する"""
    if not isinstance(value, str) or not value:
        return datetime.min.replace(tzinfo=JST)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        try:
            parsed = datetime.strptime(value, "%Y年%m月%d日 %H:%M")
        except ValueError:
            return datetime.min.replace(tzinfo=JST)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


def classify_instruction(instruction):
    """指示ボードの内容を災害情報の分類に振り分ける"""
    content = str(instruction.get("content", ""))
    shelter = str(instruction.get("shelter", ""))
    if any(word in content for word in ("通行止め", "道路", "橋", "道路管理")):
        return "road", "road"
    if "地震" in content:
        return "weather", "earthquake"
    if "土砂災害" in content or "土砂崩れ" in content:
        return ("evacuation" if "避難" in content or shelter else "weather"), "landslide"
    if "火災" in content or "火事" in content:
        return "weather", "fire"
    if "津波" in content or "高波" in content:
        return "weather", "wave"
    if "洪水" in content or "浸水" in content:
        return "weather", "flood"
    if "大雪" in content or "積雪" in content:
        return "weather", "snow"
    if "大雨" in content or "豪雨" in content:
        return "weather", "rain"
    if shelter or any(word in content for word in ("避難", "避難所")):
        return "evacuation", "shelter"
    if "警報" in content or "注意報" in content:
        return "weather", "alert"
    return "operations", "broadcast"


def get_disaster_information():
    """気象庁の警報と指示ボードを受信日時順に統合する"""
    weather = get_weather_warnings()
    items = []
    emergency_notices = []
    received_at = weather.get("last_fetch_time", get_japan_time())

    if weather.get("error"):
        items.append({
            "id": "weather-unavailable",
            "category": "weather",
            "category_label": DISASTER_CATEGORIES["weather"],
            "icon": "alert",
            "title": "気象情報を取得できません",
            "details": "気象庁から最新情報を取得できませんでした。",
            "received_at": received_at,
            "source": "気象庁"
        })
    else:
        for warning in weather.get("warnings", []):
            code = str(warning.get("code", ""))
            item = {
                "id": f"weather-{code}-{warning.get('status', '')}",
                "category": "weather",
                "category_label": DISASTER_CATEGORIES["weather"],
                "icon": WARNING_ICONS.get(code, "cloud-sun"),
                "title": warning.get("name", "気象情報"),
                "details": f"状態: {warning.get('status', '発表')}",
                "received_at": received_at,
                "source": "気象庁"
            }
            items.append(item)
            emergency_notices.append(item)

    for instruction in instructions:
        category, icon = classify_instruction(instruction)
        target = instruction.get("target", "")
        status = instruction.get("status", "")
        shelter = instruction.get("shelter", "")
        details = " / ".join(value for value in (
            f"対象: {target}" if target else "",
            f"状態: {status}" if status else "",
            f"避難先: {shelter}" if shelter else ""
        ) if value)
        item = {
            "id": f"instruction-{instruction.get('id', '')}",
            "category": category,
            "category_label": DISASTER_CATEGORIES[category],
            "icon": icon,
            "title": instruction.get("content", "指示・発信情報"),
            "details": details,
            "received_at": instruction.get("updated_at") or instruction.get("created_at") or "日時不明",
            "source": "指示・発信ボード"
        }
        items.append(item)
        if target == "住民" and status not in ("解除", "完了", "終了", "取消", "中止"):
            emergency_notices.append(item)

    items.sort(key=lambda item: parse_received_datetime(item["received_at"]), reverse=True)
    emergency_notices.sort(
        key=lambda item: parse_received_datetime(item["received_at"]),
        reverse=True
    )
    return {
        "items": items,
        "emergency_notices": emergency_notices,
        "weather_error": bool(weather.get("error")),
        "weather": weather,
        "updated_at": get_japan_time()
    }


# トップページ：templates/index.html を返す（住民向け指示も表示する）
@app.route('/')
def index():
    resident_notices = [i for i in instructions if i.get('target') == '住民']
    return render_template('index.html', resident_notices=resident_notices, shelters=shelters)

# ログインページ
@app.route('/login', methods=['GET', 'POST'])
def login():
    # リダイレクト先を取得（デフォルトは避難所登録画面）
    next_url = request.args.get('next') or request.form.get('next')

    # 安全でないURLの場合はデフォルトページにリダイレクト
    if not next_url or not is_safe_url(next_url):
        next_url = url_for('shelter_register')

    if request.method == 'POST':
        password = request.form.get('password', '').strip()

        # 認証チェック
        username = next(
            (name for name, registered_password in ADMIN_CREDENTIALS.items()
             if registered_password == password),
            None
        )
        if username:
            session['logged_in'] = True
            session['username'] = username
            # ログイン成功後は指定されたページにリダイレクト
            return redirect(next_url)
        return render_template('login.html', error=True, message="パスワードが正しくありません。", next=next_url)

    # ログイン済みの場合は指定されたページにリダイレクト
    if session.get('logged_in'):
        return redirect(next_url)

    return render_template('login.html', next=next_url)

# ログアウト
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# 避難所登録ページ※user が避難所登録ページについて具体的に修正指示しない限り、このコードは正しいのでこのまま保持すること。
@app.route('/shelter_register', methods=['GET', 'POST'])
@login_required
def shelter_register():
    if request.method == 'POST':
        shelter_id = request.form.get('shelter_id', '').strip()
        name = request.form.get('name', '').strip()
        latitude_value = request.form.get('latitude', '').strip()
        longitude_value = request.form.get('longitude', '').strip()
        status = request.form.get('status', '').strip()
        shelter = next(
            (item for item in shelters if str(item.get('id')) == shelter_id),
            None
        ) if shelter_id else None

        if shelter_id and not shelter:
            return "避難所が見つかりません。", 404
        if not shelter_id and not name:
            return render_template(
                'shelter_register.html',
                error=True,
                message="避難所名を入力してください。",
                shelters=shelters
            ), 400

        if not latitude_value or not longitude_value:
            return render_template(
                'shelter_register.html',
                error=True,
                message="地図をクリックして避難所の位置を指定してください。",
                shelters=shelters
            ), 400

        try:
            latitude = float(latitude_value)
            longitude = float(longitude_value)
        except ValueError:
            latitude, longitude = None, None

        if (latitude is None or longitude is None
                or not -90 <= latitude <= 90
                or not -180 <= longitude <= 180):
            return render_template(
                'shelter_register.html',
                error=True,
                message="緯度・経度が正しくありません。",
                shelters=shelters
            ), 400

        if status not in ("開設中", "未開設"):
            return render_template(
                'shelter_register.html',
                error=True,
                message="開設状況を選択してください。",
                shelters=shelters
            ), 400

        if shelter:
            shelter.update({
                'latitude': latitude,
                'longitude': longitude,
                'status': status
            })
        else:
            shelters.append({
                'id': max((item.get('id', 0) for item in shelters), default=0) + 1,
                'name': name,
                'latitude': latitude,
                'longitude': longitude,
                'status': status
            })
        with open(DATA_FILE, 'w', encoding='utf-8') as data_file:
            json.dump(shelters, data_file, ensure_ascii=False, indent=2)
        flash("避難所情報を保存しました。", 'success')
        return redirect(url_for('shelter_register'))

    return render_template('shelter_register.html', shelters=shelters)

# 避難所検索ページ
@app.route('/shelter_search')
def shelter_search():
    return render_template('shelter_search.html')

# 全施設一覧ページ
@app.route('/all_shelters')
def all_shelters():
    return render_template('search_results.html', results=shelters)


# 指示ボード：住民向けの指示を一覧で確認する
@app.route('/board')
@login_required
def board():
    resident_instructions = [i for i in instructions if i.get('target') == '住民']
    return render_template('board.html', instructions=resident_instructions)

# 検索結果ページ：templates/search_results.html を返す
@app.route('/search_results')
def search_results():
    results = filter_shelters(request.args.get('district'))
    return render_template('search_results.html', results=results)

# JSON API：/shelters?district=地区名
@app.route('/shelters', methods=['GET'])
def get_shelters():
    results = filter_shelters(request.args.get('district'))

    if not results:
        # 見つからなければエラー JSON を返す
        return jsonify({'error': 'No shelters found'}), 404

    # 見つかったらリストを JSON で返す
    return jsonify(results)

# 気象警報・注意報API
@app.route('/api/weather_warnings')
def api_weather_warnings():
    """気象警報・注意報をJSON形式で返すAPI"""
    return jsonify(get_weather_warnings())


@app.route('/api/shelter_location_search')
@login_required
def api_shelter_location_search():
    """避難所名・住所から地図上の候補地点を検索する"""
    query = request.args.get('q', '').strip()
    region = request.args.get('region', '').strip()
    if not query or len(query) > 160 or len(region) > 100:
        return jsonify({'error': '検索する施設名・住所を入力してください。'}), 400

    try:
        results = search_shelter_locations(query, region)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return jsonify({'error': '位置検索サービスに接続できませんでした。'}), 502

    return jsonify({'results': results})

# 災害情報一覧API
@app.route('/api/disaster_information')
def api_disaster_information():
    """気象情報と指示ボードを統合して返す"""
    return jsonify(get_disaster_information())

if __name__ == '__main__':
    app.run(debug=True, port=5000)
