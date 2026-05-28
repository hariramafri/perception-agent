import os
import shutil
import json
import time
import argparse
import re
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
from telemetry import TelemetryAgent

# --- 1. LOGGING MODULE ---
def setup_telemetry(output_dir):
    return TelemetryAgent(output_dir)

# --- 2. NAVIGATION MODULE ---
def navigate_to_url(driver, url, telemetry):
    with telemetry.track_action("URL Navigation & Stabilization"):
        try:
            driver.get(url)
            
            # Wait for DOMContentLoaded equivalent
            WebDriverWait(driver, 30).until(
                lambda d: d.execute_script("return document.readyState") in ["interactive", "complete"]
            )
            
            # Stabilize URL (Anti-Fail Logic) to handle complex redirects
            last_url = driver.current_url
            stable_count = 0
            for _ in range(15):
                time.sleep(0.5)
                current = driver.current_url
                if current == last_url:
                    stable_count += 1
                    if stable_count >= 4:  # URL hasn't changed for 2 seconds
                        break
                else:
                    stable_count = 0
                    last_url = current
                    
            # Wait for networkidle equivalent (readyState == complete + buffer)
            WebDriverWait(driver, 30).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
            time.sleep(2)
            
            final_url = driver.current_url
            if final_url != url:
                telemetry.log("INFO", f"Redirect resolved. Final stable URL: {final_url}")
            else:
                telemetry.log("INFO", f"Final destination URL: {final_url}")
                
            return final_url
        except Exception as e:
            telemetry.log("WARN", f"Navigation error/timeout: {e}")
            return driver.current_url

def clean_slug(value, max_len=40):
    value = re.sub(r'\s+', ' ', value or '').strip()
    value = re.sub(r'[^a-zA-Z0-9]+', '_', value.lower()).strip('_')
    return value[:max_len].rstrip('_')

def should_include_context(base_slug, context_slug, data):
    if not context_slug or context_slug in base_slug:
        return False
    if "transition_alert" in context_slug:
        return False
    if data.get("y", 0) < 120:
        return False
    return len(base_slug) <= 25

def build_semantic_name(data):
    tag_name = data["tagName"]
    inner_text = re.sub(r'\s+', ' ', data.get("innerText", "")).strip()
    aria_label = re.sub(r'\s+', ' ', data.get("ariaLabel", "")).strip()
    context_hint = data.get("contextHint", "")

    base_source = inner_text or aria_label or data.get("name") or data.get("id") or tag_name
    base_name = clean_slug(base_source)
    if not base_name:
        base_name = tag_name

    context_slug = clean_slug(context_hint)
    if should_include_context(base_name, context_slug, data):
        base_name = f"{context_slug}_{base_name}"

    if tag_name not in base_name:
        base_name = f"{base_name}_{tag_name}"

    return base_name[:50].rstrip('_')

# --- 3. EXTRACTION MODULE ---
def extract_visible_elements(driver, output_dir, telemetry, full_screenshot_path, offset_index=0):
    observations = []
    
    with telemetry.track_action(f"Elements Extraction (Bulk JS)"):
        js_script = r"""
        function isBadContextText(text) {
            return !text || text.indexOf('International Shopping Transition Alert') !== -1;
        }

        function getContext(el) {
            if (el.getBoundingClientRect().top + window.scrollY < 120) return '';

            let parent = el.parentElement;
            for (let i = 0; i < 5; i++) {
                if (!parent || parent.tagName === 'BODY') break;
                let h = parent.querySelector('h1, h2, h3, h4, h5, h6, [class*="title"], [class*="name"], [class*="product"]');
                if (h && h !== el && !el.contains(h) && h.innerText && h.innerText.trim()) {
                    let text = h.innerText.trim();
                    if (!isBadContextText(text) && text.length > 0 && text.length < 60) return text;
                }
                parent = parent.parentElement;
            }
            parent = el.parentElement;
            for (let i = 0; i < 4; i++) {
                if (!parent || parent.tagName === 'BODY') break;
                let text = parent.innerText ? parent.innerText.trim().split('\\n')[0] : '';
                if (!isBadContextText(text) && text && text !== el.innerText.trim() && text.length > 0 && text.length < 60) {
                    return text;
                }
                parent = parent.parentElement;
            }
            return '';
        }

        function isElementVisible(el) {
            if (!el.offsetWidth || !el.offsetHeight) return false;
            var style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
            
            var rect = el.getBoundingClientRect();
            if (rect.width < 5 || rect.height < 5) return false;
            
            // Check out of bounds coordinates
            if (rect.right < 0 || rect.bottom < 0) return false;
            if (rect.left > Math.max(document.documentElement.scrollWidth, window.innerWidth)) return false;
            if (rect.top > Math.max(document.documentElement.scrollHeight, window.innerHeight)) return false;
            
            // Semantic Check
            var hasText = el.innerText && el.innerText.trim().length > 0;
            var hasAria = el.getAttribute('aria-label') && el.getAttribute('aria-label').trim().length > 0;
            var hasId = el.id && el.id.trim().length > 0;
            var hasName = el.name && el.name.trim().length > 0;
            var isInput = el.tagName.toLowerCase() === 'input' || el.tagName.toLowerCase() === 'textarea' || el.tagName.toLowerCase() === 'select';
            if (!hasText && !hasAria && !hasId && !hasName && !isInput) return false;
            
            var parent = el.parentElement;
            while (parent && parent !== document.body && parent !== document.documentElement) {
                var parentStyle = window.getComputedStyle(parent);
                if (parentStyle.display === 'none' || parentStyle.visibility === 'hidden' || parentStyle.opacity === '0') return false;
                
                if (parentStyle.overflow !== 'visible') {
                    var parentRect = parent.getBoundingClientRect();
                    // If element is completely outside the parent's bounding box
                    if (rect.bottom <= parentRect.top || rect.top >= parentRect.bottom || rect.right <= parentRect.left || rect.left >= parentRect.right) {
                        return false;
                    }
                }
                parent = parent.parentElement;
            }
            return true;
        }

        function getAbsXPath(element) {
            if (element.tagName.toLowerCase() == 'html')
                return '/html[1]';
            if (element === document.body)
                return '/html[1]/body[1]';
            var ix = 0;
            var siblings = element.parentNode.childNodes;
            for (var i = 0; i < siblings.length; i++) {
                var sibling = siblings[i];
                if (sibling === element)
                    return getAbsXPath(element.parentNode) + '/' + element.tagName.toLowerCase() + '[' + (ix + 1) + ']';
                if (sibling.nodeType === 1 && sibling.tagName === element.tagName)
                    ix++;
            }
            return '';
        }

        function isStable(str) {
            if (!str) return false;
            if (str.match(/[0-9a-f]{8}-[0-9a-f]{4}/i)) return false;
            if (str.match(/(mui-|css-|react-|jss)/i)) return false;
            if (str.match(/-\\w{5,}$/)) return false;
            if (str.match(/\\d{5,}/)) return false;
            return true;
        }

        function getRelXPath(element) {
            if (element.id && isStable(element.id)) {
                return "//" + element.tagName.toLowerCase() + "[@id='" + element.id + "']";
            }
            var testid = element.getAttribute('data-testid');
            if (testid) {
                return "//" + element.tagName.toLowerCase() + "[@data-testid='" + testid + "']";
            }
            if (element.name && isStable(element.name)) {
                return "//" + element.tagName.toLowerCase() + "[@name='" + element.name + "']";
            }
            
            var paths = [];
            for (var el = element; el && el.nodeType === 1; el = el.parentNode) {
                if (el.tagName.toLowerCase() === 'html') break;
                
                var tag = el.tagName.toLowerCase();
                var part = tag;
                
                if (el.id && isStable(el.id)) {
                    part += "[@id='" + el.id + "']";
                    paths.unshift("//" + part);
                    return paths.join('');
                }
                
                var className = el.getAttribute('class');
                if (className) {
                    var classes = className.split(/\\s+/).filter(isStable);
                    if (classes.length > 0) {
                        part += "[contains(@class, '" + classes[0] + "')]";
                    }
                }
                
                var index = 1;
                for (var sibling = el.previousSibling; sibling; sibling = sibling.previousSibling) {
                    if (sibling.nodeType === 1 && sibling.tagName === el.tagName) {
                        index++;
                    }
                }
                if (index > 1 || el.nextElementSibling) {
                    part += "[" + index + "]";
                }
                
                paths.unshift("/" + part);
            }
            return paths.length ? "/" + paths.join('') : "";
        }

        function getCssPath(el) {
            if (!(el instanceof Element)) return '';
            var path = [];
            while (el.nodeType === Node.ELEMENT_NODE) {
                var selector = el.nodeName.toLowerCase();
                if (el.id && isStable(el.id)) {
                    selector += '#' + el.id;
                    path.unshift(selector);
                    break;
                } else {
                    var className = el.getAttribute('class');
                    if (className) {
                        var classes = className.split(/\\s+/).filter(isStable);
                        if (classes.length > 0) {
                            selector += '.' + classes[0];
                        }
                    }
                    var sib = el, nth = 1;
                    while (sib = sib.previousElementSibling) {
                        if (sib.nodeName.toLowerCase() == el.nodeName.toLowerCase())
                           nth++;
                    }
                    if (nth != 1) selector += ":nth-of-type("+nth+")";
                }
                path.unshift(selector);
                el = el.parentNode;
            }
            return path.join(" > ");
        }

        var selectors = "button, input, a, select, textarea, [role='button'], [tabindex]:not([tabindex='-1'])";
        var elements = document.querySelectorAll(selectors);
        var results = [];
        for (var i = 0; i < elements.length; i++) {
            var el = elements[i];
            
            if (!isElementVisible(el)) continue;
            
            var rect = el.getBoundingClientRect();
            
            results.push({
                index: i,
                tagName: el.tagName.toLowerCase(),
                innerText: el.innerText ? el.innerText.trim() : '',
                id: el.id || '',
                className: typeof el.className === 'string' ? el.className : (el.getAttribute('class') || ''),
                name: el.name || el.getAttribute('name') || '',
                ariaLabel: el.getAttribute('aria-label') || '',
                contextHint: getContext(el),
                absXPath: getAbsXPath(el),
                relXPath: getRelXPath(el),
                cssPath: getCssPath(el),
                x: rect.left + window.scrollX,
                y: rect.top + window.scrollY,
                width: rect.width,
                height: rect.height
            });
        }
        return results;
        """
        elements_data = driver.execute_script(js_script)
        elements_data = sorted(
            elements_data,
            key=lambda item: (round(float(item.get("y", 0)) / 8) * 8, float(item.get("x", 0)))
        )
        
    with telemetry.track_action(f"Cropping {len(elements_data)} Element Screenshots"):
        try:
            from PIL import Image
            full_image = Image.open(full_screenshot_path)
            device_pixel_ratio = driver.execute_script("return window.devicePixelRatio || 1;")
            scale_x = device_pixel_ratio or 1
            scale_y = device_pixel_ratio or 1
            
            used_names = set()
            
            for idx, data in enumerate(elements_data):
                try:
                    x, y, w, h = data['x'], data['y'], data['width'], data['height']
                    
                    left = max(0, int(x * scale_x) - 2)
                    top = max(0, int(y * scale_y) - 2)
                    right = min(full_image.width, int((x + w) * scale_x) + 2)
                    bottom = min(full_image.height, int((y + h) * scale_y) + 2)

                    if right <= left or bottom <= top:
                        telemetry.log("WARN", f"Skipping invalid crop bounds for element {idx}: {(left, top, right, bottom)}")
                        continue
                    
                    tag_name = data["tagName"]
                    inner_text = data["innerText"]
                    aria_label = data["ariaLabel"]
                    context_hint = data.get("contextHint", "")
                    base_name = build_semantic_name(data)
                    
                    semantic_name = base_name
                    if semantic_name in used_names:
                        semantic_name = f"{base_name}_order_{idx}"
                    
                    # Cap at 50 chars
                    semantic_name = semantic_name[:50].rstrip('_')
                    
                    # Hard collision check if it's still duplicated due to truncation
                    collision_counter = 1
                    while semantic_name in used_names:
                        suffix = f"_{collision_counter}"
                        semantic_name = semantic_name[:50 - len(suffix)].rstrip('_') + suffix
                        collision_counter += 1
                        
                    used_names.add(semantic_name)
                    
                    page_order = offset_index + idx + 1
                    element_img_path = os.path.join(output_dir, f"{page_order:04d}_{semantic_name}.png")
                    cropped = full_image.crop((left, top, right, bottom))
                    if cropped.getbbox() is None:
                        telemetry.log("WARN", f"Skipping empty crop for element {idx}: {semantic_name}")
                        continue
                    cropped.save(element_img_path)
                    
                    observations.append({
                        "element_index": idx,
                        "page_order": page_order,
                        "semantic_name": semantic_name,
                        "tag_name": tag_name,
                        "inner_text": inner_text,
                        "attributes": {
                            "id": data["id"],
                            "class": data["className"],
                            "name": data["name"],
                            "aria-label": aria_label
                        },
                        "context_hint": context_hint,
                        "abs_xpath": data.get("absXPath", ""),
                        "rel_xpath": data.get("relXPath", ""),
                        "css_path": data.get("cssPath", ""),
                        "bounding_box": {
                            "x": x,
                            "y": y,
                            "width": w,
                            "height": h
                        },
                        "screenshot_path": element_img_path
                    })
                except Exception:
                    continue
        except Exception as e:
            telemetry.log("ERROR", f"Failed to crop images: {e}")
                
    return observations

def extract_dom(driver, url, output_dir, telemetry, full_screenshot_path):
    with telemetry.track_action("DOM Node Count Check"):
        node_count = driver.execute_script("return document.querySelectorAll('*').length;")
        telemetry.log("INFO", f"Total DOM nodes: {node_count}")

    observations = []
    start_time = time.time()
    TIMEOUT = 30.0

    if node_count > 2000:
        telemetry.log("INFO", "Large DOM detected (>2000 nodes). Performing lazy-load scroll chunks.")
        
        viewport_height = driver.execute_script("return window.innerHeight;")
        total_height = driver.execute_script("return document.body.scrollHeight;")
        
        chunks = int(total_height / viewport_height) + 1
        for i in range(chunks):
            if time.time() - start_time > TIMEOUT:
                telemetry.log("WARN", "Lazy load timeout (30s) reached. Proceeding with extraction.")
                break
                
            driver.execute_script(f"window.scrollTo(0, {i * viewport_height});")
            time.sleep(0.5) # Allow lazy loaded elements to render
            
        # Scroll back to top to ensure consistent screenshot coordinates
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(1)

    with telemetry.track_action("Screenshot Capture (Post-Stabilization Full Page)"):
        total_height = driver.execute_script("return Math.max(document.body.scrollHeight, document.body.offsetHeight, document.documentElement.clientHeight, document.documentElement.scrollHeight, document.documentElement.offsetHeight);")
        driver.set_window_size(1920, total_height + 100)
        time.sleep(0.5)
        driver.save_screenshot(full_screenshot_path)

    observations = extract_visible_elements(driver, output_dir, telemetry, full_screenshot_path)
        
    return observations

def main_scraper(url: str, output_dir: str = "output"):
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    telemetry = setup_telemetry(output_dir)
    
    options = Options()
    options.add_argument('--headless')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--disable-gpu')
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    telemetry.set_driver(driver)
    
    try:
        final_url = navigate_to_url(driver, url, telemetry)
        full_screenshot_path = os.path.join(output_dir, "full_page.png")
        observations = extract_dom(driver, final_url, output_dir, telemetry, full_screenshot_path)
        
        json_report_path = os.path.join(output_dir, "dom_observation.json")
        with open(json_report_path, "w", encoding="utf-8") as f:
            json.dump({
                "url": final_url,
                "full_page_screenshot": full_screenshot_path,
                "total_interactive_elements": len(observations),
                "elements": observations
            }, f, indent=4)
            
        telemetry.log("INFO", f"Extraction complete! Saved to {json_report_path}")
        
    finally:
        driver.quit()

def looks_like_plain_url(value):
    value = (value or "").strip()
    return bool(re.fullmatch(r"https?://[^\s]+", value))

def run_perception_agent(user_instruction: str, output_dir: str = "output", headed: bool = False):
    """
    Perception-agent entry point.

    A plain URL keeps the original single-page perception flow.
    A natural-language instruction is handed to the execution agent, which asks
    perception to recapture pages whenever navigation or uncertainty occurs.
    """
    if looks_like_plain_url(user_instruction):
        main_scraper(user_instruction, output_dir)
        return {
            "mode": "perception_only",
            "output_dir": output_dir
        }

    from execution_agent import ExecutionAgent

    agent = ExecutionAgent(output_dir, headless=not headed)
    result = agent.run(user_instruction)
    return {
        "mode": "perception_guided_execution",
        **result
    }

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Perception Agent: scrape a URL or coordinate execution from user instructions")
    parser.add_argument("instruction", help="A URL, or an instruction such as: \"open https://example.com then click Learn more\"")
    parser.add_argument("--output", "-o", default="output", help="Output directory")
    parser.add_argument("--headed", action="store_true", help="Show Chrome during instruction execution")
    args = parser.parse_args()
    result = run_perception_agent(args.instruction, args.output, headed=args.headed)
    if result["mode"] == "perception_guided_execution":
        print("\nPerception-guided execution complete.")
        print(f"Run folder: {result['run_dir']}")
        print(f"Workbook: {result['workbook']}")
        print(f"Captured pages: {len(result['pages'])}")
    else:
        print("\nPerception capture complete.")
        print(f"Output folder: {result['output_dir']}")
