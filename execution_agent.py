import argparse
import html
import json
import os
import re
import time
import zipfile
from datetime import datetime
from urllib.parse import urlparse

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from dom_scraper import extract_dom, navigate_to_url, setup_telemetry, extract_app_name, get_next_run_dir
from generate_report import build_report_rows, generate_report


def safe_slug(value, max_len=45):
    value = re.sub(r"\s+", " ", value or "").strip()
    value = re.sub(r"[^a-zA-Z0-9]+", "_", value.lower()).strip("_")
    return (value or "page")[:max_len].rstrip("_")


def sheet_name(value, used):
    base = re.sub(r"[\[\]\:\*\?\/\\]", " ", value or "Page").strip() or "Page"
    base = re.sub(r"\s+", " ", base)[:31]
    name = base
    counter = 2
    while name.lower() in used:
        suffix = f" {counter}"
        name = f"{base[:31 - len(suffix)]}{suffix}"
        counter += 1
    used.add(name.lower())
    return name


def extract_first_url(instructions):
    match = re.search(r"https?://[^\s\"']+", instructions)
    if match:
        return match.group(0).rstrip(".,)")
    return ""


def split_steps(instructions):
    normalized = re.sub(r"\bthen\b", "\n", instructions, flags=re.IGNORECASE)
    normalized = re.sub(r"\band then\b", "\n", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+\band\s+(?=(?:click|type|enter|fill|press|wait|capture|scan|perceive)\b)", "\n", normalized, flags=re.IGNORECASE)
    parts = re.split(r"[\n;]+", normalized)
    return [p.strip(" .") for p in parts if p.strip(" .")]


def parse_quoted_text(text):
    match = re.search(r"['\"]([^'\"]+)['\"]", text)
    return match.group(1) if match else ""


def parse_steps(instructions):
    url = extract_first_url(instructions)
    actions = []
    for step in split_steps(instructions):
        lowered = step.lower()
        if "http://" in lowered or "https://" in lowered:
            continue
        if lowered.startswith(("open ", "go to ", "navigate to ")):
            continue
        if lowered.startswith(("type ", "enter ", "fill ")):
            value = parse_quoted_text(step)
            target_match = re.search(r"\b(?:into|in|on)\s+(.+)$", step, flags=re.IGNORECASE)
            target = target_match.group(1).strip() if target_match else ""
            if not value:
                value_match = re.search(r"^(?:type|enter|fill)\s+(.+?)(?:\s+\b(?:into|in|on)\b|$)", step, flags=re.IGNORECASE)
                value = value_match.group(1).strip() if value_match else ""
            actions.append({"action": "type", "target": target, "value": value, "raw": step})
            continue
        if lowered.startswith("click ") or " click " in lowered:
            target = re.sub(r"^.*?\bclick(?: on)?\s+", "", step, flags=re.IGNORECASE).strip()
            actions.append({"action": "click", "target": target, "raw": step})
            continue
        if lowered.startswith("press "):
            key = re.sub(r"^press\s+", "", step, flags=re.IGNORECASE).strip()
            actions.append({"action": "press", "key": key, "raw": step})
            continue
        if lowered.startswith("wait"):
            actions.append({"action": "wait", "raw": step})
            continue
        if lowered.startswith(("capture", "scan", "perceive")):
            actions.append({"action": "capture", "raw": step})
            continue
        actions.append({"action": "unknown", "raw": step})
    return url, actions


def setup_driver(headless=False):
    options = Options()
    options.add_argument("--start-maximized")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def stabilize(driver, timeout=20):
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: d.execute_script("return document.readyState") == "complete"
        )
    except WebDriverException:
        pass
    
    # Wait for dynamic DOM node count stability (for AJAX popups/tab loading)
    try:
        start_time = time.time()
        last_count = -1
        stable_since = time.time()
        while time.time() - start_time < 5.0:
            current_count = driver.execute_script("return document.querySelectorAll('*').length;")
            if current_count != last_count:
                last_count = current_count
                stable_since = time.time()
            elif time.time() - stable_since >= 0.8:
                break
            time.sleep(0.2)
    except Exception:
        time.sleep(1)


def page_label(driver):
    title = ""
    try:
        title = driver.title
    except WebDriverException:
        title = ""
    if title:
        return title
    try:
        parsed = urlparse(driver.current_url)
        return parsed.netloc or parsed.path or "page"
    except WebDriverException:
        return "page"


def compute_page_fingerprint(driver):
    try:
        html_length = driver.execute_script("return document.documentElement.outerHTML.length;")
        node_count = driver.execute_script("return document.querySelectorAll('*').length;")
        return f"{node_count}:{html_length}"
    except WebDriverException:
        return ""


def read_observation(page_dir):
    with open(os.path.join(page_dir, "dom_observation.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_query_terms(query):
    query = re.sub(r"[^a-zA-Z0-9]+", " ", query.lower()).strip()
    # Remove stop words to prevent diluting the match score
    stopwords = {"the", "a", "an", "on", "in", "to", "click", "type", "press", "enter", "into", "for"}
    clean_query = " ".join([w for w in re.sub(r"[^a-zA-Z0-9]+", " ", query.lower()).split() if w not in stopwords])
    
    aliases = {
        "bar": "input field box textbox textarea searchbox combobox",
        "box": "input field textbox textarea searchbox combobox",
        "field": "input textbox textarea",
        "search": "search q query find lookup",
        "button": "button submit",
        "login": "login log in signin sign in",
        "signin": "login log in signin sign in",
        "cart": "cart basket checkout",
    }
    terms = query.split()
    expanded = terms[:]
    terms = clean_query.split()
    expanded = []
    for term in terms:
        expanded.append(term)
        expanded.extend(aliases.get(term, "").split())
    # Remove duplicates while preserving order
    return " ".join(dict.fromkeys(expanded)).strip()


def element_search_text(element):
    attrs = element.get("attributes", {})
    haystack_parts = [
        element.get("semantic_name", ""),
        element.get("tag_name", ""),
        element.get("inner_text", ""),
        element.get("context_hint", ""),
        attrs.get("aria-label", ""),
        attrs.get("alt", ""),
        attrs.get("title", ""),
        attrs.get("role", ""),
        attrs.get("data-testid", ""),
        attrs.get("id", ""),
        attrs.get("name", ""),
        attrs.get("class", ""),
    ]
    return re.sub(r"[^a-zA-Z0-9]+", " ", " ".join(haystack_parts).lower()).strip()


def is_text_entry_element(element):
    tag = element.get("tag_name", "").lower()
    attrs = element.get("attributes", {})
    role = attrs.get("role", "").lower()
    input_type = attrs.get("type", "").lower()
    if tag in {"textarea", "select"}:
        return True
    if tag == "input" and input_type not in {"button", "submit", "checkbox", "radio", "hidden", "image"}:
        return True
    return role in {"textbox", "searchbox", "combobox"}


def is_clickable_element(element):
    tag = element.get("tag_name", "").lower()
    role = element.get("attributes", {}).get("role", "").lower()
    return tag in {"a", "button", "input", "select", "summary", "details"} or role in {"button", "link", "menuitem", "option", "checkbox", "radio", "tab", "switch", "combobox"}


def score_element(element, query, action_type=None):
    original_query = re.sub(r"[^a-zA-Z0-9]+", " ", query.lower()).strip()
    clean_original_tokens = [
        t for t in original_query.split()
        if t not in {"the", "a", "an", "on", "in", "to", "click", "type", "press", "enter", "into", "for"}
    ]
    clean_original_query = " ".join(clean_original_tokens)
    query_norm = normalize_query_terms(query)

    if not original_query:
        return 0

    haystack = element_search_text(element)
    if not haystack:
        return 0

    inner_text = re.sub(r"[^a-zA-Z0-9]+", " ", element.get("inner_text", "").lower()).strip()
    aria_label = re.sub(r"[^a-zA-Z0-9]+", " ", element.get("attributes", {}).get("aria-label", "").lower()).strip()

    base_score = 0
    if clean_original_query and (clean_original_query == inner_text or clean_original_query == aria_label):
        base_score = 100
    elif clean_original_query and (clean_original_query in inner_text or clean_original_query in aria_label):
        base_score = 95
    elif query_norm and query_norm in haystack:
        base_score = 85
    elif original_query and original_query in haystack:
        base_score = 80
    elif clean_original_query and clean_original_query in haystack:
        base_score = 75
    else:
        query_tokens = set(query_norm.split())
        haystack_tokens = set(haystack.split())
        if not query_tokens:
            return 0
        overlap = len(query_tokens & haystack_tokens)
        base_score = int((overlap / max(1, len(query_tokens))) * 70)

    # Compute score for primary_target if prepositions exist
    prep_match = re.split(r"\s+\b(?:from|in|on|at|under|of|inside|within|for)\b\s+", query, flags=re.IGNORECASE)
    if prep_match and len(prep_match) > 1:
        primary_target = prep_match[0].strip()
        clean_primary = re.sub(r"[^a-zA-Z0-9]+", " ", primary_target.lower()).strip()
        clean_primary_tokens = [
            t for t in clean_primary.split()
            if t not in {"the", "a", "an", "on", "in", "to", "click", "type", "press", "enter", "into", "for"}
        ]
        clean_primary_query = " ".join(clean_primary_tokens)
        primary_query_norm = normalize_query_terms(primary_target)
        
        primary_score = 0
        if clean_primary_query and (clean_primary_query == inner_text or clean_primary_query == aria_label):
            primary_score = 100
        elif clean_primary_query and (clean_primary_query in inner_text or clean_primary_query in aria_label):
            primary_score = 95
        elif primary_query_norm and primary_query_norm in haystack:
            primary_score = 85
        elif clean_primary and clean_primary in haystack:
            primary_score = 80
        elif clean_primary_query and clean_primary_query in haystack:
            primary_score = 75
        else:
            primary_tokens = set(primary_query_norm.split())
            haystack_tokens = set(haystack.split())
            if primary_tokens:
                overlap = len(primary_tokens & haystack_tokens)
                primary_score = int((overlap / max(1, len(primary_tokens))) * 70)
                
        if primary_score == 0:
            base_score = 0
        else:
            base_score = max(base_score, primary_score)

    if clean_original_query and base_score == 0:
        return 0

    attrs = element.get("attributes", {})
    tag = element.get("tag_name", "").lower()
    role = attrs.get("role", "").lower()
    original_tokens = set(original_query.split())

    if action_type == "type":
        if is_text_entry_element(element):
            base_score += 45
        else:
            base_score -= 35

        if "search" in original_tokens and (
            attrs.get("name", "").lower() == "q"
            or "search" in attrs.get("aria-label", "").lower()
            or "search" in attrs.get("title", "").lower()
            or role in {"searchbox", "combobox"}
            or tag == "textarea"
        ):
            base_score += 25

    elif action_type == "click":
        if is_clickable_element(element):
            base_score += 25
        else:
            base_score -= 25

        if clean_original_query == "search":
            if attrs.get("name", "").lower() == "btnk" or attrs.get("aria-label", "").lower() == "google search":
                base_score += 40
            if tag == "input" and attrs.get("role", "").lower() == "button":
                base_score += 25
            if "image" in haystack or "images" in haystack:
                base_score -= 35

        if "cart" in clean_original_query and (
            "cart" in haystack
            or "cart" in attrs.get("id", "").lower()
            or "cart" in attrs.get("class", "").lower()
            or "cart" in attrs.get("name", "").lower()
        ):
            base_score += 20
        if "view" in clean_original_query and "cart" in clean_original_query and "view cart" in haystack:
            base_score += 20
        if clean_original_query and clean_original_query in element.get("semantic_name", ""):
            base_score += 10

    return min(100, max(0, base_score))


def find_best_element(elements, target, min_score=45, action_type=None):
    if action_type in {"type", "click"}:
        min_score = 20

    def element_priority_score(el):
        tag = el.get("tag_name", "").lower()
        role = el.get("attributes", {}).get("role", "").lower()
        bbox = el.get("bounding_box", {})
        area = float(bbox.get("width", 0)) * float(bbox.get("height", 0)) if bbox else 0

        priority = 0
        if tag in {"button", "a", "input", "select", "textarea"}:
            priority = 50
        elif role in {"button", "link", "menuitem", "option", "checkbox", "radio", "tab", "switch", "combobox"}:
            priority = 40
        return (priority, area)

    scored = [(score_element(el, target, action_type), el) for el in elements]
    scored.sort(
        key=lambda item: (item[0], element_priority_score(item[1])[0], element_priority_score(item[1])[1]),
        reverse=True,
    )

    if not scored:
        return None, scored[:5]

    if scored[0][0] >= min_score:
        return scored[0][1], scored[:5]

    normalized_target = re.sub(r"[^a-zA-Z0-9]+", " ", target.lower()).strip()
    target_tokens = [t for t in normalized_target.split() if t]

    def token_match(el):
        haystack = element_search_text(el)
        return all(token in haystack for token in target_tokens)

    if action_type in {"click", "type"}:
        for score, el in scored[:10]:
            if score >= 15 and is_clickable_element(el) and token_match(el):
                return el, scored[:5]
        for score, el in scored[:10]:
            if score >= 10 and is_clickable_element(el):
                return el, scored[:5]

    return None, scored[:5]


def selenium_candidates(element):
    attrs = element.get("attributes", {})
    candidates = []
    if element.get("abs_xpath"):
        candidates.append((By.XPATH, element["abs_xpath"]))
    if attrs.get("id"):
        candidates.append((By.ID, attrs["id"]))
    if attrs.get("name"):
        candidates.append((By.NAME, attrs["name"]))
    if element.get("rel_xpath"):
        candidates.append((By.XPATH, element["rel_xpath"]))
    if element.get("css_path"):
        candidates.append((By.CSS_SELECTOR, element["css_path"]))
    if attrs.get("data-testid"):
        candidates.append((By.CSS_SELECTOR, f"[data-testid='{attrs['data-testid']}']"))
    return candidates


def xpath_literal(text):
    if "'" not in text:
        return f"'{text}'"
    if '"' not in text:
        return f'"{text}"'
    parts = text.split("'")
    return "concat(" + ", \"'\", ".join(f"'{part}'" for part in parts) + ")"


def resolve_web_element(driver, element):
    errors = []
    for by, value in selenium_candidates(element):
        try:
            found = driver.find_element(by, value)
            return found
        except WebDriverException as exc:
            errors.append(f"{by}={value}: {exc.__class__.__name__}")

    attrs = element.get("attributes", {})
    element_texts = []
    if element.get("inner_text"):
        element_texts.append(element["inner_text"].strip())
    if attrs.get("aria-label"):
        element_texts.append(attrs["aria-label"].strip())
    if attrs.get("title"):
        element_texts.append(attrs["title"].strip())
    if attrs.get("data-testid"):
        element_texts.append(attrs["data-testid"].strip())
    if attrs.get("name"):
        element_texts.append(attrs["name"].strip())
    if element.get("semantic_name"):
        element_texts.append(element["semantic_name"].replace("_", " ").strip())

    for text in element_texts:
        if not text:
            continue
        escaped_text = xpath_literal(text)
        xpath_queries = [
            f"//*[normalize-space(string(.))={escaped_text}]",
            f"//*[contains(normalize-space(string(.)), {escaped_text})]",
        ]
        for xpath_query in xpath_queries:
            try:
                matches = driver.find_elements(By.XPATH, xpath_query)
                for match in matches:
                    if match.is_displayed():
                        return match
            except WebDriverException as exc:
                errors.append(f"xpath={xpath_query}: {exc.__class__.__name__}")

    # Fallback: search visible clickable elements by the target text derived from the element metadata.
    text_tokens = [t for t in re.sub(r"[^a-zA-Z0-9]+", " ", " ".join(element_texts).lower()).split() if t]
    if text_tokens:
        try:
            js = """
            const tokens = arguments[0];
            const normalize = (s) => (s || '').replace(/[^a-zA-Z0-9]+/g, ' ').toLowerCase().trim();
            const isVisible = (el) => {
                if (!el.offsetWidth || !el.offsetHeight) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const rect = el.getBoundingClientRect();
                return rect.width >= 1 && rect.height >= 1;
            };
            const candidates = Array.from(document.querySelectorAll('button, a, input, textarea, select, [role="button"], [role="link"], [role="menuitem"], [role="option"], [role="checkbox"], [role="radio"], [role="tab"], [role="switch"], [contenteditable]'));
            for (const el of candidates) {
                if (!isVisible(el)) continue;
                let text = normalize(el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                if (!text) continue;
                let matchesAll = tokens.every(token => text.includes(token));
                if (matchesAll) return el;
            }
            return null;
            """
            fallback_element = driver.execute_script(js, text_tokens)
            if fallback_element:
                return fallback_element
        except WebDriverException as exc:
            errors.append(f"js_fallback={exc.__class__.__name__}")

    raise RuntimeError("; ".join(errors) or "No usable locator available")


def click_element(driver, element):
    web_element = resolve_web_element(driver, element)
    
    # Check if element is already in the viewport to avoid unnecessary scrolling
    is_in_viewport = False
    try:
        is_in_viewport = driver.execute_script("""
            const el = arguments[0];
            const rect = el.getBoundingClientRect();
            return (
                rect.top >= 0 &&
                rect.left >= 0 &&
                rect.bottom <= (window.innerHeight || document.documentElement.clientHeight) &&
                rect.right <= (window.innerWidth || document.documentElement.clientWidth)
            );
        """, web_element)
    except Exception:
        pass

    if not is_in_viewport:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", web_element)
            time.sleep(0.2)
        except Exception:
            pass

    try:
        web_element.click()
    except WebDriverException:
        # Fallback for click interception or detached elements
        driver.execute_script("arguments[0].click();", web_element)


def type_into_element(driver, element, value):
    web_element = resolve_web_element(driver, element)
    
    # Check if element is already in the viewport to avoid unnecessary scrolling
    is_in_viewport = False
    try:
        is_in_viewport = driver.execute_script("""
            const el = arguments[0];
            const rect = el.getBoundingClientRect();
            return (
                rect.top >= 0 &&
                rect.left >= 0 &&
                rect.bottom <= (window.innerHeight || document.documentElement.clientHeight) &&
                rect.right <= (window.innerWidth || document.documentElement.clientWidth)
            );
        """, web_element)
    except Exception:
        pass

    if not is_in_viewport:
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", web_element)
            time.sleep(0.2)
        except Exception:
            pass

    try:
        web_element.clear()
    except WebDriverException:
        web_element.send_keys(Keys.CONTROL, "a")
    web_element.send_keys(value)


def key_from_name(key_name):
    key_name = key_name.strip().lower()
    mapping = {
        "enter": Keys.ENTER,
        "return": Keys.ENTER,
        "tab": Keys.TAB,
        "escape": Keys.ESCAPE,
        "esc": Keys.ESCAPE,
        "space": Keys.SPACE,
    }
    return mapping.get(key_name, key_name)


def xlsx_col_name(index):
    name = ""
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def xlsx_cell(value, row_idx, col_idx):
    ref = f"{xlsx_col_name(col_idx)}{row_idx}"
    text = html.escape(str(value if value is not None else ""))
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def write_xlsx(path, sheets):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
""" + "".join(
            f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for i in range(1, len(sheets) + 1)
        ) + "</Types>")
        zf.writestr("_rels/.rels", """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""")
        workbook_sheets = []
        workbook_rels = []
        for i, sheet in enumerate(sheets, start=1):
            workbook_sheets.append(
                f'<sheet name="{html.escape(sheet["name"])}" sheetId="{i}" r:id="rId{i}"/>'
            )
            workbook_rels.append(
                f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
            )
        zf.writestr("xl/workbook.xml", """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>""" + "".join(workbook_sheets) + "</sheets></workbook>")
        zf.writestr("xl/_rels/workbook.xml.rels", """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">""" + "".join(workbook_rels) + "</Relationships>")
        for i, sheet in enumerate(sheets, start=1):
            rows = sheet["rows"]
            headers = []
            for row in rows:
                for key in row.keys():
                    if key not in headers:
                        headers.append(key)
            headers = headers or ["Status"]
            xml_rows = []
            all_rows = [dict(zip(headers, headers))] + rows
            for row_idx, row in enumerate(all_rows, start=1):
                cells = [xlsx_cell(row.get(header, ""), row_idx, col_idx) for col_idx, header in enumerate(headers, start=1)]
                xml_rows.append(f'<row r="{row_idx}">{"".join(cells)}</row>')
            zf.writestr(f"xl/worksheets/sheet{i}.xml", """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData>""" + "".join(xml_rows) + "</sheetData></worksheet>")


class ExecutionAgent:
    def __init__(self, output_root, headless=False):
        self.output_root = output_root
        self.headless = headless
        self.run_dir = None
        self.pages_dir = None
        self.telemetry = None
        self.driver = None
        self.pages = []
        self.execution_log = []
        self.agent_handoffs = []
        self.current_data = None
        self.current_page_dir = None

    def start(self):
        os.makedirs(self.pages_dir, exist_ok=True)
        self.driver = setup_driver(headless=self.headless)
        self.telemetry.set_driver(self.driver)

    def stop(self):
        if self.driver:
            if not self.headless:
                print("\nHeaded browser open for 5 seconds for visual verification...")
                time.sleep(5)
            self.driver.quit()

    def capture_page(self, reason):
        self.log_agent_switch("Execution Agent", "Perception Agent", f"Capture requested: {reason}")
        label = page_label(self.driver)
        page_number = len(self.pages) + 1
        folder = f"{page_number:02d}_{safe_slug(label)}"
        page_dir = os.path.join(self.pages_dir, folder)
        suffix = 2
        while os.path.exists(page_dir):
            page_dir = os.path.join(self.pages_dir, f"{folder}_{suffix}")
            suffix += 1
        os.makedirs(page_dir, exist_ok=True)

        full_screenshot_path = os.path.join(page_dir, "full_page.png")
        observations = extract_dom(self.driver, self.driver.current_url, page_dir, self.telemetry, full_screenshot_path)
        fingerprint = compute_page_fingerprint(self.driver)
        data = {
            "url": self.driver.current_url,
            "title": label,
            "capture_reason": reason,
            "full_page_screenshot": full_screenshot_path,
            "total_interactive_elements": len(observations),
            "elements": observations,
            "fingerprint": fingerprint,
        }
        json_path = os.path.join(page_dir, "dom_observation.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        generate_report(json_path, page_dir)

        page_record = {
            "page_number": page_number,
            "title": label,
            "url": self.driver.current_url,
            "reason": reason,
            "folder": page_dir,
            "json": json_path,
        }
        self.pages.append(page_record)
        self.current_data = data
        self.current_page_dir = page_dir
        self.log_agent_switch("Perception Agent", "Execution Agent", f"Capture complete: {label}")
        return page_record

    @property
    def current_url_safe(self):
        if not self.driver:
            return ""
        from selenium.common.exceptions import NoSuchWindowException
        try:
            return self.driver.current_url
        except NoSuchWindowException:
            try:
                handles = self.driver.window_handles
                if handles:
                    self.driver.switch_to.window(handles[0])
                    return self.driver.current_url
            except Exception:
                pass
        except Exception:
            pass
        return ""

    def log_agent_switch(self, from_agent, to_agent, reason):
        event = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "status": "agent_switch",
            "from_agent": from_agent,
            "to_agent": to_agent,
            "reason": reason,
            "url": self.current_url_safe,
        }
        self.agent_handoffs.append(event)
        self.execution_log.append(event)
        if hasattr(self, "telemetry") and self.telemetry:
            self.telemetry.log("AGENT_SHIFT", f"{from_agent} -> {to_agent}: {reason}")

    def log(self, status, message, action=None, element=None):
        if hasattr(self, "telemetry") and self.telemetry:
            level = "INFO" if status == "ok" else "WARN"
            self.telemetry.log(level, f"Execution Agent: {message}")
        self.execution_log.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "status": status,
            "message": message,
            "action": action or {},
            "matched_element": element.get("semantic_name") if element else "",
            "url": self.current_url_safe,
        })

    def switch_to_new_window_if_any(self):
        if not self.driver:
            return False
        
        from selenium.common.exceptions import NoSuchWindowException
        
        try:
            handles = self.driver.window_handles
            current = self.driver.current_window_handle
        except NoSuchWindowException:
            try:
                handles = self.driver.window_handles
                if handles:
                    self.driver.switch_to.window(handles[0])
                    self.current_data = None
                    self.log("ok", f"Current window was closed. Switched back to first window: {handles[0]}", {"action": "switch_window"})
                    return True
            except Exception:
                pass
            return False
        except Exception:
            return False

        if len(handles) > 1 and handles[-1] != current:
            new_handle = handles[-1]
            try:
                self.driver.switch_to.window(new_handle)
                new_url = self.driver.current_url
                
                is_ad = any(kw in new_url.lower() for kw in [
                    "googleads", "adsystem", "popads", "doubleclick", 
                    "clickserve", "adservice", "googlesyndication", "adclick",
                    "about:blank", "ad-delivery", "advertising"
                ])
                if is_ad:
                    self.driver.close()
                    self.driver.switch_to.window(current)
                    self.log("ok", f"Closed auto-opened ad/popup window: {new_url}", {"action": "close_ad_window"})
                    return False
                else:
                    self.log("ok", f"Switched to new browser window/tab {new_handle}: {new_url}", {"action": "switch_window"})
                    self.current_data = None
                    return True
            except Exception:
                try:
                    self.driver.switch_to.window(current)
                except Exception:
                    pass
        return False

    def ensure_perception(self, reason):
        if not self.driver:
            return

        stabilize(self.driver)
        self.switch_to_new_window_if_any()

        if not self.current_data:
            self.capture_page(reason)
            return

        current_url = self.driver.current_url
        current_title = page_label(self.driver)
        current_fingerprint = compute_page_fingerprint(self.driver)
        if (
            current_url != self.current_data.get("url")
            or current_title != self.current_data.get("title")
            or (current_fingerprint and current_fingerprint != self.current_data.get("fingerprint"))
        ):
            self.current_data = None
            self.capture_page(reason)
            
    def handle_popups(self):
        from dom_scraper import dismiss_ad_popups
        if dismiss_ad_popups(self.driver, self.telemetry):
            self.current_data = None
            self.ensure_perception("re-perceiving after popup dismissal")

    def execute_action(self, action):
        previous_url = self.driver.current_url
        previous_title = page_label(self.driver)
        if action["action"] == "wait":
            time.sleep(2)
            self.log("ok", "Waited for page stabilization.", action)
            return True
        if action["action"] == "capture":
            self.capture_page(action.get("raw", "manual capture"))
            self.log("ok", "Captured current page perception.", action)
            return True
        if action["action"] == "press":
            self.ensure_perception("before action")
            self.handle_popups()
            try:
                self.driver.switch_to.active_element.send_keys(key_from_name(action.get("key", "")))
                stabilize(self.driver)
                self.log("ok", f"Pressed {action.get('key')}.", action)
            except Exception as e:
                self.log("needs_perception_guidance", f"Press action failed: {e}", action)
                return False
        elif action["action"] in {"click", "type"}:
            self.ensure_perception("before action")
            self.handle_popups()
            target = action.get("target", "")
            self.log_agent_switch("Execution Agent", "Perception Agent", f"Resolve target for action: {action.get('raw', action['action'])}")
            element, candidates = find_best_element(self.current_data.get("elements", []), target, action_type=action["action"])
            if element:
                top_score = candidates[0][0] if candidates else ""
                self.log_agent_switch("Perception Agent", "Execution Agent", f"Resolved '{target}' to {element.get('semantic_name')} with score {top_score}")
            if not element:
                self.current_data = None
                self.capture_page(f"confusion: could not resolve {target}")
                self.log(
                    "needs_perception_guidance",
                    f"Could not confidently resolve target '{target}'. Top candidates: {[(c[0], c[1].get('semantic_name')) for c in candidates]}",
                    action,
                )
                return False
            try:
                if action["action"] == "click":
                    click_element(self.driver, element)
                    message = f"Clicked {element.get('semantic_name')}."
                else:
                    type_into_element(self.driver, element, action.get("value", ""))
                    message = f"Typed into {element.get('semantic_name')}."
                stabilize(self.driver)
                self.switch_to_new_window_if_any()
                self.log("ok", message, action, element)
            except Exception as exc:
                self.current_data = None
                self.capture_page(f"confusion: action failed for {target}")
                self.log("needs_perception_guidance", f"Action failed: {exc}", action, element)
                return False
        else:
            self.capture_page(f"confusion: unknown instruction {action.get('raw', '')}")
            self.log("needs_perception_guidance", f"Unknown instruction: {action.get('raw', '')}", action)
            return False

        # After most page interactions, refresh the current perception if the page state changed.
        if action["action"] in {"click", "type", "press"}:
            self.ensure_perception("after action")

        return True

    def write_outputs(self):
        used_sheet_names = set()
        sheets = []
        for page in self.pages:
            with open(page["json"], "r", encoding="utf-8") as f:
                data = json.load(f)
            rows = build_report_rows(data)
            for row in rows:
                row["Page URL"] = page["url"]
                row["Page Folder"] = page["folder"]
            sheets.append({
                "name": sheet_name(page["title"] or page["url"], used_sheet_names),
                "rows": rows or [{"Status": "No elements found"}],
            })
        if self.execution_log:
            sheets.append({
                "name": sheet_name("Execution Log", used_sheet_names),
                "rows": self.execution_log,
            })
        if self.agent_handoffs:
            sheets.append({
                "name": sheet_name("Agent Handoffs", used_sheet_names),
                "rows": self.agent_handoffs,
            })
        workbook_path = os.path.join(self.run_dir, "perception_execution_report.xlsx")
        write_xlsx(workbook_path, sheets)
        with open(os.path.join(self.run_dir, "execution_log.json"), "w", encoding="utf-8") as f:
            json.dump(self.execution_log, f, indent=4)
        with open(os.path.join(self.run_dir, "agent_handoffs.json"), "w", encoding="utf-8") as f:
            json.dump(self.agent_handoffs, f, indent=4)
        with open(os.path.join(self.run_dir, "page_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(self.pages, f, indent=4)
        return workbook_path

    def run(self, instructions):
        url, actions = parse_steps(instructions)
        if not url:
            raise ValueError("No starting URL found in instructions. Include a full http:// or https:// URL.")
        app_name = extract_app_name(url)
        self.run_dir = get_next_run_dir(self.output_root, app_name)
        self.pages_dir = os.path.join(self.run_dir, "pages")
        self.telemetry = setup_telemetry(self.run_dir)

        print("\n" + "="*60)
        print("INSTRUCTION PLAN:")
        print(f"  Starting URL: {url}")
        for idx, action in enumerate(actions, 1):
            if action["action"] == "click":
                print(f"  Action {idx}: Click on '{action['target']}'")
            elif action["action"] == "type":
                print(f"  Action {idx}: Type '{action['value']}' into '{action['target']}'")
            elif action["action"] == "press":
                print(f"  Action {idx}: Press key '{action['key']}'")
            elif action["action"] == "wait":
                print(f"  Action {idx}: Wait for page stabilization")
            elif action["action"] == "capture":
                print(f"  Action {idx}: Capture page snapshot")
            else:
                print(f"  Action {idx}: Unknown action '{action.get('raw', '')}'")
        print("="*60 + "\n")

        self.start()
        try:
            self.log_agent_switch("Perception Agent", "Execution Agent", "Instruction requires browser execution.")
            final_url = navigate_to_url(self.driver, url, self.telemetry)
            self.log("ok", f"Opened starting URL: {final_url}", {"action": "open", "target": url})
            self.capture_page("initial navigation")
            
            for idx, action in enumerate(actions, 1):
                print(f"\n--- [Executing Action {idx}/{len(actions)}] {action.get('raw', '')} ---")
                success = self.execute_action(action)
                if not success:
                    failure_reason = "Unknown failure"
                    if self.execution_log:
                        last_event = self.execution_log[-1]
                        if last_event.get("status") != "ok":
                            failure_reason = last_event.get("message", failure_reason)
                    
                    print("\n" + "="*60)
                    print("EXECUTION ERROR: ACTION FAILED!")
                    print(f"  Failed Step {idx}: {action.get('raw', '')}")
                    print(f"  Reason: {failure_reason}")
                    print(f"  Run Directory: {self.run_dir}")
                    print("="*60 + "\n")
                    
                    try:
                        self.write_outputs()
                    except Exception:
                        pass
                    
                    raise RuntimeError(f"Execution failed at step {idx} ('{action.get('raw', '')}'): {failure_reason}")

            workbook_path = self.write_outputs()
            return {
                "run_dir": self.run_dir,
                "workbook": workbook_path,
                "pages": self.pages,
                "execution_log": self.execution_log,
            }
        finally:
            self.stop()


def main():
    parser = argparse.ArgumentParser(description="Execution agent that drives actions and captures perception per page.")
    parser.add_argument("instructions", help="Natural language instruction, including the starting URL.")
    parser.add_argument("--output", "-o", default="output", help="Base output directory.")
    parser.add_argument("--headed", action="store_true", default=True, help="Run Chrome visibly instead of headless.")
    args = parser.parse_args()

    agent = ExecutionAgent(args.output, headless=not args.headed)
    result = agent.run(args.instructions)
    print("\nExecution complete.")
    print(f"Run folder: {result['run_dir']}")
    print(f"Workbook: {result['workbook']}")
    print(f"Captured pages: {len(result['pages'])}")
    needs_guidance = [item for item in result["execution_log"] if item["status"] == "needs_perception_guidance"]
    if needs_guidance:
        print(f"Guidance needed: {len(needs_guidance)} item(s). See execution_log.json.")


if __name__ == "__main__":
    main()
