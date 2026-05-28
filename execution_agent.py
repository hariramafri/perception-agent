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

from dom_scraper import extract_dom, navigate_to_url, setup_telemetry
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
        if lowered.startswith("click ") or " click " in lowered:
            target = re.sub(r"^.*?\bclick(?: on)?\s+", "", step, flags=re.IGNORECASE).strip()
            actions.append({"action": "click", "target": target, "raw": step})
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


def setup_driver(headless=True):
    options = Options()
    if headless:
        options.add_argument("--headless")
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


def read_observation(page_dir):
    with open(os.path.join(page_dir, "dom_observation.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def score_element(element, query):
    query = re.sub(r"[^a-zA-Z0-9]+", " ", query.lower()).strip()
    if not query:
        return 0
    attrs = element.get("attributes", {})
    haystack_parts = [
        element.get("semantic_name", ""),
        element.get("inner_text", ""),
        element.get("context_hint", ""),
        attrs.get("aria-label", ""),
        attrs.get("id", ""),
        attrs.get("name", ""),
    ]
    haystack = re.sub(r"[^a-zA-Z0-9]+", " ", " ".join(haystack_parts).lower()).strip()
    if not haystack:
        return 0
    if query == haystack:
        return 100
    if query in haystack:
        return 80 + min(15, len(query))
    query_tokens = set(query.split())
    haystack_tokens = set(haystack.split())
    if not query_tokens:
        return 0
    overlap = len(query_tokens & haystack_tokens)
    return int((overlap / len(query_tokens)) * 70)


def find_best_element(elements, target, min_score=45):
    scored = sorted(
        ((score_element(el, target), el) for el in elements),
        key=lambda item: item[0],
        reverse=True,
    )
    if not scored or scored[0][0] < min_score:
        return None, scored[:5]
    return scored[0][1], scored[:5]


def selenium_candidates(element):
    attrs = element.get("attributes", {})
    candidates = []
    if attrs.get("id"):
        candidates.append((By.ID, attrs["id"]))
    if attrs.get("name"):
        candidates.append((By.NAME, attrs["name"]))
    if element.get("rel_xpath"):
        candidates.append((By.XPATH, element["rel_xpath"]))
    if element.get("abs_xpath"):
        candidates.append((By.XPATH, element["abs_xpath"]))
    if element.get("css_path"):
        candidates.append((By.CSS_SELECTOR, element["css_path"]))
    return candidates


def resolve_web_element(driver, element):
    errors = []
    for by, value in selenium_candidates(element):
        try:
            found = driver.find_element(by, value)
            return found
        except WebDriverException as exc:
            errors.append(f"{by}={value}: {exc.__class__.__name__}")
    raise RuntimeError("; ".join(errors) or "No usable locator available")


def click_element(driver, element):
    web_element = resolve_web_element(driver, element)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", web_element)
    time.sleep(0.2)
    web_element.click()


def type_into_element(driver, element, value):
    web_element = resolve_web_element(driver, element)
    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", web_element)
    time.sleep(0.2)
    web_element.clear()
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
            headers = list(rows[0].keys()) if rows else ["Status"]
            xml_rows = []
            all_rows = [dict(zip(headers, headers))] + rows
            for row_idx, row in enumerate(all_rows, start=1):
                cells = [xlsx_cell(row.get(header, ""), row_idx, col_idx) for col_idx, header in enumerate(headers, start=1)]
                xml_rows.append(f'<row r="{row_idx}">{"".join(cells)}</row>')
            zf.writestr(f"xl/worksheets/sheet{i}.xml", """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheetData>""" + "".join(xml_rows) + "</sheetData></worksheet>")


class ExecutionAgent:
    def __init__(self, output_root, headless=True):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = os.path.join(output_root, f"execution_run_{timestamp}")
        self.pages_dir = os.path.join(self.run_dir, "pages")
        self.output_root = output_root
        self.headless = headless
        self.telemetry = setup_telemetry(self.run_dir)
        self.driver = None
        self.pages = []
        self.execution_log = []
        self.current_data = None
        self.current_page_dir = None

    def start(self):
        os.makedirs(self.pages_dir, exist_ok=True)
        self.driver = setup_driver(headless=self.headless)
        self.telemetry.set_driver(self.driver)

    def stop(self):
        if self.driver:
            self.driver.quit()

    def capture_page(self, reason):
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
        data = {
            "url": self.driver.current_url,
            "title": label,
            "capture_reason": reason,
            "full_page_screenshot": full_screenshot_path,
            "total_interactive_elements": len(observations),
            "elements": observations,
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
        return page_record

    def log(self, status, message, action=None, element=None):
        self.execution_log.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "status": status,
            "message": message,
            "action": action or {},
            "matched_element": element.get("semantic_name") if element else "",
            "url": self.driver.current_url if self.driver else "",
        })

    def ensure_perception(self, reason):
        if not self.current_data:
            self.capture_page(reason)

    def execute_action(self, action):
        previous_url = self.driver.current_url
        previous_title = page_label(self.driver)
        if action["action"] == "wait":
            time.sleep(2)
            self.log("ok", "Waited for page stabilization.", action)
            return
        if action["action"] == "capture":
            self.capture_page(action.get("raw", "manual capture"))
            self.log("ok", "Captured current page perception.", action)
            return
        if action["action"] == "press":
            self.driver.switch_to.active_element.send_keys(key_from_name(action.get("key", "")))
            stabilize(self.driver)
            self.log("ok", f"Pressed {action.get('key')}.", action)
        elif action["action"] in {"click", "type"}:
            self.ensure_perception("before action")
            target = action.get("target", "")
            element, candidates = find_best_element(self.current_data.get("elements", []), target)
            if not element:
                self.capture_page(f"confusion: could not resolve {target}")
                self.log(
                    "needs_perception_guidance",
                    f"Could not confidently resolve target '{target}'. Top candidates: {[c[1].get('semantic_name') for c in candidates]}",
                    action,
                )
                return
            try:
                if action["action"] == "click":
                    click_element(self.driver, element)
                    message = f"Clicked {element.get('semantic_name')}."
                else:
                    type_into_element(self.driver, element, action.get("value", ""))
                    message = f"Typed into {element.get('semantic_name')}."
                stabilize(self.driver)
                self.log("ok", message, action, element)
            except Exception as exc:
                self.capture_page(f"confusion: action failed for {target}")
                self.log("needs_perception_guidance", f"Action failed: {exc}", action, element)
                return
        else:
            self.capture_page(f"confusion: unknown instruction {action.get('raw', '')}")
            self.log("needs_perception_guidance", f"Unknown instruction: {action.get('raw', '')}", action)
            return

        current_url = self.driver.current_url
        current_title = page_label(self.driver)
        if current_url != previous_url or current_title != previous_title:
            self.capture_page(f"after action: {action.get('raw', action['action'])}")

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
        workbook_path = os.path.join(self.run_dir, "perception_execution_report.xlsx")
        write_xlsx(workbook_path, sheets)
        with open(os.path.join(self.run_dir, "execution_log.json"), "w", encoding="utf-8") as f:
            json.dump(self.execution_log, f, indent=4)
        with open(os.path.join(self.run_dir, "page_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(self.pages, f, indent=4)
        return workbook_path

    def run(self, instructions):
        url, actions = parse_steps(instructions)
        if not url:
            raise ValueError("No starting URL found in instructions. Include a full http:// or https:// URL.")
        self.start()
        try:
            final_url = navigate_to_url(self.driver, url, self.telemetry)
            self.log("ok", f"Opened starting URL: {final_url}", {"action": "open", "target": url})
            self.capture_page("initial navigation")
            for action in actions:
                self.execute_action(action)
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
    parser.add_argument("--headed", action="store_true", help="Run Chrome visibly instead of headless.")
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
