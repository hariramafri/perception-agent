import os
import shutil
import json
import time
import argparse
import re
import base64
from datetime import datetime
from urllib.parse import urlparse
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
    alt_text = re.sub(r'\s+', ' ', data.get("alt", "")).strip()
    title_text = re.sub(r'\s+', ' ', data.get("title", "")).strip()
    child_image_text = re.sub(r'\s+', ' ', data.get("childImageText", "")).strip()
    context_hint = data.get("contextHint", "")

    base_source = inner_text or aria_label or alt_text or title_text or child_image_text or data.get("name") or data.get("id") or tag_name
    base_name = clean_slug(base_source)
    if not base_name:
        base_name = tag_name

    context_slug = clean_slug(context_hint)
    if should_include_context(base_name, context_slug, data):
        base_name = f"{context_slug}_{base_name}"

    if tag_name not in base_name:
        base_name = f"{base_name}_{tag_name}"

    return base_name[:50].rstrip('_')

def element_priority(data):
    tag_name = data.get("tagName", "")
    role = data.get("role", "")
    if tag_name in {"input", "select", "textarea", "button"}:
        return 100
    if tag_name == "a":
        return 90
    if role in {"button", "link", "menuitem", "option", "checkbox", "radio", "tab", "switch", "textbox", "searchbox", "combobox"}:
        return 85
    if tag_name in {"label", "summary", "details"}:
        return 75
    if tag_name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return 65
    if tag_name == "img":
        return 40
    return 30

def overlap_ratio_of_smaller(first, second):
    ax1, ay1 = float(first.get("x", 0)), float(first.get("y", 0))
    ax2, ay2 = ax1 + float(first.get("width", 0)), ay1 + float(first.get("height", 0))
    bx1, by1 = float(second.get("x", 0)), float(second.get("y", 0))
    bx2, by2 = bx1 + float(second.get("width", 0)), by1 + float(second.get("height", 0))
    inter_w = max(0, min(ax2, bx2) - max(ax1, bx1))
    inter_h = max(0, min(ay2, by2) - max(ay1, by1))
    inter_area = inter_w * inter_h
    first_area = max(1, float(first.get("width", 0)) * float(first.get("height", 0)))
    second_area = max(1, float(second.get("width", 0)) * float(second.get("height", 0)))
    return inter_area / min(first_area, second_area)

def dedupe_overlapping_elements(elements_data):
    prioritized = sorted(
        elements_data,
        key=lambda item: (
            -element_priority(item),
            -(float(item.get("width", 0)) * float(item.get("height", 0))),
            float(item.get("y", 0)),
            float(item.get("x", 0)),
        )
    )
    kept = []
    for candidate in prioritized:
        if any(overlap_ratio_of_smaller(candidate, existing) >= 0.88 for existing in kept):
            continue
        kept.append(candidate)
    return sorted(
        kept,
        key=lambda item: (round(float(item.get("y", 0)) / 8) * 8, float(item.get("x", 0)))
    )


def capture_full_page_screenshot(driver, path, telemetry):
    try:
        width = driver.execute_script(
            "return Math.max(document.body.scrollWidth, document.documentElement.scrollWidth, document.body.offsetWidth, document.documentElement.offsetWidth, document.documentElement.clientWidth);"
        )
        height = driver.execute_script(
            "return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight, document.body.offsetHeight, document.documentElement.offsetHeight, document.documentElement.clientHeight);"
        )
        device_pixel_ratio = driver.execute_script("return window.devicePixelRatio || 1;")

        if width < 1 or height < 1:
            raise ValueError(f"Invalid full page dimensions: {width}x{height}")

        driver.execute_cdp_cmd("Emulation.setDeviceMetricsOverride", {
            "width": int(width),
            "height": int(height),
            "deviceScaleFactor": float(device_pixel_ratio),
            "mobile": False,
            "screenOrientation": {"angle": 0, "type": "portraitPrimary"}
        })

        screenshot = driver.execute_cdp_cmd("Page.captureScreenshot", {
            "fromSurface": True,
            "captureBeyondViewport": True
        })
        driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})

        with open(path, "wb") as f:
            f.write(base64.b64decode(screenshot["data"]))
        telemetry.log("INFO", f"Captured full page screenshot {width}x{height}.")
        return True
    except Exception as exc:
        telemetry.log("WARN", f"Full page screenshot via CDP failed: {exc}. Falling back to viewport screenshot.")
        try:
            result = driver.save_screenshot(path)
            if result:
                telemetry.log("INFO", "Fallback viewport screenshot saved.")
                return True
            telemetry.log("WARN", "Fallback screenshot returned false.")
        except Exception as fallback_exc:
            telemetry.log("ERROR", f"Fallback screenshot failed: {fallback_exc}")
        return False


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
            var tag = el.tagName.toLowerCase();
            var role = el.getAttribute('role') || '';
            var hasText = el.innerText && el.innerText.trim().length > 0;
            var hasAria = el.getAttribute('aria-label') && el.getAttribute('aria-label').trim().length > 0;
            var hasId = el.id && el.id.trim().length > 0;
            var hasName = el.name && el.name.trim().length > 0;
            var hasAlt = el.getAttribute('alt') && el.getAttribute('alt').trim().length > 0;
            var hasTitle = el.getAttribute('title') && el.getAttribute('title').trim().length > 0;
            var hasRole = el.getAttribute('role') && el.getAttribute('role').trim().length > 0;
            var hasOnClick = el.getAttribute('onclick') || (style.cursor === 'pointer');
            var hasDataLocator = el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-cy');
            var isInput = tag === 'input' || tag === 'textarea' || tag === 'select';
            var isSemanticTag = ['img', 'label', 'summary', 'details', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'].indexOf(tag) !== -1;
            var isActionRole = ['button', 'link', 'menuitem', 'option', 'checkbox', 'radio', 'tab', 'switch', 'textbox', 'searchbox', 'combobox'].indexOf(role) !== -1;
            if ((tag === 'div' || tag === 'span') && el.innerText && el.innerText.trim().length > 160 && !isActionRole && !hasOnClick && !hasDataLocator) return false;
            if (!hasText && !hasAria && !hasId && !hasName && !hasAlt && !hasTitle && !hasRole && !hasOnClick && !hasDataLocator && !isInput && !isSemanticTag) return false;
            
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

        var selectors = [
            "button",
            "input",
            "a",
            "select",
            "textarea",
            "summary",
            "details",
            "label",
            "img[alt]",
            "img[title]",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "[onclick]",
            "[contenteditable='true']",
            "[data-testid]",
            "[data-test]",
            "[data-cy]",
            "[aria-label]",
            "[title]",
            "[role='button']",
            "[role='link']",
            "[role='menuitem']",
            "[role='option']",
            "[role='checkbox']",
            "[role='radio']",
            "[role='tab']",
            "[role='switch']",
            "[role='textbox']",
            "[role='searchbox']",
            "[role='combobox']",
            "[role='img']",
            "[tabindex]:not([tabindex='-1'])"
        ].join(", ");
        var elements = document.querySelectorAll(selectors);
        var results = [];
        for (var i = 0; i < elements.length; i++) {
            var el = elements[i];
            
            if (!isElementVisible(el)) continue;
            
            var rect = el.getBoundingClientRect();
            var childImage = el.querySelector ? el.querySelector('img[alt], img[title]') : null;
            
            results.push({
                index: i,
                tagName: el.tagName.toLowerCase(),
                innerText: el.innerText ? el.innerText.trim() : '',
                id: el.id || '',
                className: typeof el.className === 'string' ? el.className : (el.getAttribute('class') || ''),
                name: el.name || el.getAttribute('name') || '',
                ariaLabel: el.getAttribute('aria-label') || '',
                alt: el.getAttribute('alt') || '',
                title: el.getAttribute('title') || '',
                childImageText: childImage ? (childImage.getAttribute('alt') || childImage.getAttribute('title') || '') : '',
                role: el.getAttribute('role') || '',
                testId: el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-cy') || '',
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
        before_dedupe = len(elements_data)
        elements_data = dedupe_overlapping_elements(elements_data)
        removed_duplicates = before_dedupe - len(elements_data)
        if removed_duplicates:
            telemetry.log("INFO", f"Removed {removed_duplicates} overlapping duplicate elements.")
        
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
                    screenshot_path = ""
                    
                    left = max(0, int(x * scale_x) - 2)
                    top = max(0, int(y * scale_y) - 2)
                    right = min(full_image.width, int((x + w) * scale_x) + 2)
                    bottom = min(full_image.height, int((y + h) * scale_y) + 2)

                    if right <= left or bottom <= top:
                        telemetry.log("WARN", f"Skipping invalid crop bounds for element {idx}: {(left, top, right, bottom)}")
                    else:
                        cropped = full_image.crop((left, top, right, bottom))
                        if cropped.getbbox() is None:
                            telemetry.log("WARN", f"Skipping empty crop for element {idx}: {semantic_name}")
                        else:
                            cropped.save(element_img_path)
                            screenshot_path = element_img_path
                    
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
                            "aria-label": aria_label,
                            "alt": data.get("alt", ""),
                            "title": data.get("title", ""),
                            "role": data.get("role", ""),
                            "data-testid": data.get("testId", "")
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
                        "screenshot_path": screenshot_path
                    })
                except Exception:
                    continue
        except Exception as e:
            telemetry.log("ERROR", f"Failed to crop images: {e}")
                
    return observations

def dismiss_ad_popups(driver, telemetry):
    """
    Detects and dismisses common overlay popups and Google Vignette/interstitial ads.
    Returns True if an ad or popup was dismissed, False otherwise.
    """
    telemetry.log("INFO", "Checking for advertisement pop-ups or overlays...")
    
    ad_dismissed = False
    
    # 1. Check for native browser alert dialogs first
    try:
        alert = driver.switch_to.alert
        alert.dismiss()
        telemetry.log("INFO", "Dismissed browser alert popup.")
        ad_dismissed = True
    except Exception:
        pass

    # 2. Check for Google Vignette/Ad iframes
    if not ad_dismissed:
        try:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            for iframe in iframes:
                try:
                    iframe_id = iframe.get_attribute("id") or ""
                    iframe_name = iframe.get_attribute("name") or ""
                    if any(kw in iframe_id.lower() or kw in iframe_name.lower() for kw in ["aswift", "google", "ad"]):
                        driver.switch_to.frame(iframe)
                        
                        # Check for nested ad_iframe inside
                        try:
                            nested_iframe = driver.find_element(By.ID, "ad_iframe")
                            driver.switch_to.frame(nested_iframe)
                        except Exception:
                            # Not nested
                            pass
                        
                        # Look for close/dismiss button
                        dismiss_btn = None
                        selectors = [
                            "#dismiss-button", 
                            ".dismiss-button", 
                            "[aria-label*='Close']", 
                            "[aria-label*='close']",
                            "div[id='dismiss-button']",
                            "span[id='dismiss-button']",
                            "button[id='dismiss-button']"
                        ]
                        for sel in selectors:
                            try:
                                btn = driver.find_element(By.CSS_SELECTOR, sel)
                                if btn.is_displayed():
                                    dismiss_btn = btn
                                    break
                            except Exception:
                                pass
                                
                        if not dismiss_btn:
                            for tag in ["div", "button", "span", "a"]:
                                try:
                                    elements = driver.find_elements(By.TAG_NAME, tag)
                                    for el in elements:
                                        text = el.text.strip().lower()
                                        if el.is_displayed() and text in ["close", "dismiss", "skip", "no thanks", "x"]:
                                            dismiss_btn = el
                                            break
                                    if dismiss_btn:
                                        break
                                except Exception:
                                    pass
                                    
                        if dismiss_btn:
                            driver.execute_script("arguments[0].click();", dismiss_btn)
                            telemetry.log("INFO", "Google Vignette advertisement dismissed.")
                            ad_dismissed = True
                            driver.switch_to.default_content()
                            time.sleep(1.5)  # Let overlay close and page stabilize
                            break
                        else:
                            driver.switch_to.default_content()
                except Exception:
                    try:
                        driver.switch_to.default_content()
                    except Exception:
                        pass
        except Exception as e:
            telemetry.log("WARN", f"Error scanning for Google Vignette iframes: {e}")

    # 3. Check for HTML modal/overlay ad popups in main context (JS Smasher)
    if not ad_dismissed:
        try:
            js_smasher = """
            const keywords = ['cancel', 'close', 'dismiss', 'no thanks', 'not now', 'decline', 'accept all', 'accept cookies', 'got it', 'reject all', 'maybe later', 'skip', 'x'];
            let modals = Array.from(document.querySelectorAll('dialog, [role="dialog"], [role="alertdialog"], .modal, .popup, .overlay, .banner, [id*="modal"], [id*="banner"], [id*="cookie"], [class*="modal"], [class*="popup"], [class*="ad-"], [id*="ad-"]'));
            for (let modal of modals) {
                if (modal.offsetWidth > 0 && modal.offsetHeight > 0) {
                    let btns = Array.from(modal.querySelectorAll('button, a, input, [role="button"]'));
                    for (let btn of btns) {
                        let txt = (btn.innerText || btn.value || '').trim().toLowerCase();
                        let aria = (btn.getAttribute('aria-label') || '').trim().toLowerCase();
                        if (keywords.includes(txt) || keywords.includes(aria) || txt === 'x' || aria === 'x') {
                            btn.click();
                            return true;
                        }
                    }
                }
            }
            let btns = Array.from(document.querySelectorAll('button, a, input, [role="button"]'));
            for (let i = btns.length - 1; i >= 0; i--) {
                let btn = btns[i];
                if (btn.offsetWidth > 0 && btn.offsetHeight > 0) {
                    let style = window.getComputedStyle(btn);
                    if (style.position === 'fixed' || style.position === 'absolute' || style.zIndex > 100) {
                        let txt = (btn.innerText || btn.value || '').trim().toLowerCase();
                        let aria = (btn.getAttribute('aria-label') || '').trim().toLowerCase();
                        if (keywords.includes(txt) || keywords.includes(aria) || txt === 'x' || aria === 'x') {
                            btn.click();
                            return true;
                        }
                    }
                }
            }
            return false;
            """
            if driver.execute_script(js_smasher):
                telemetry.log("INFO", "Dismissed modal/overlay popup dynamically via JS.")
                ad_dismissed = True
                time.sleep(1.5)
        except Exception as e:
            telemetry.log("WARN", f"Error executing HTML popup JS smasher: {e}")

    if ad_dismissed:
        try:
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass
            
    return ad_dismissed

def is_modal_open(driver):
    try:
        js = """
        if (document.body.classList.contains('modal-open')) return true;
        const modalSelectors = [
            '.modal.in', '.modal.show', 
            '[role="dialog"]', '[role="alertdialog"]', 
            '.fade.show', '.modal-backdrop',
            '#cartModal', 
            '.checkout-modal', '.popup-container', '.overlay-container'
        ];
        for (const selector of modalSelectors) {
            const elements = document.querySelectorAll(selector);
            for (const el of elements) {
                if (el.offsetWidth > 0 && el.offsetHeight > 0) {
                    const style = window.getComputedStyle(el);
                    if (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0') {
                        if (el.tagName !== 'BODY' && el.tagName !== 'HTML') {
                            return true;
                        }
                    }
                }
            }
        }
        return false;
        """
        return bool(driver.execute_script(js))
    except Exception:
        return False


def extract_dom(driver, url, output_dir, telemetry, full_screenshot_path):
    dismiss_ad_popups(driver, telemetry)
    with telemetry.track_action("DOM Node Count Check"):
        node_count = driver.execute_script("return document.querySelectorAll('*').length;")
        telemetry.log("INFO", f"Total DOM nodes: {node_count}")

    observations = []
    start_time = time.time()
    TIMEOUT = 30.0

    modal_active = is_modal_open(driver)
    if modal_active:
        telemetry.log("INFO", "Active modal popup detected. Viewport-only screenshot mode enabled.")

    if not modal_active and node_count > 2000:
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
        if modal_active:
            try:
                result = driver.save_screenshot(full_screenshot_path)
                if result:
                    telemetry.log("INFO", "Viewport screenshot captured successfully for modal.")
                else:
                    telemetry.log("WARN", "Viewport screenshot failed for modal.")
            except Exception as e:
                telemetry.log("ERROR", f"Viewport screenshot failed: {e}")
        else:
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(0.5)
            capture_full_page_screenshot(driver, full_screenshot_path, telemetry)

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

def extract_app_name(url):
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc or parsed.path
        netloc = netloc.split(":")[0]
        if netloc.startswith("www."):
            netloc = netloc[4:]
        parts = netloc.split(".")
        if parts:
            app_name = parts[0]
            if not app_name:
                app_name = "app"
            app_name = re.sub(r"[^a-zA-Z0-9]+", "_", app_name.lower())
            return app_name or "app"
    except Exception:
        pass
    return "app"

def get_next_run_dir(output_root, app_name):
    date_str = datetime.now().strftime("%Y-%m-%d")
    date_dir = os.path.join(output_root, date_str)
    os.makedirs(date_dir, exist_ok=True)
    
    max_count = 0
    pattern = re.compile(rf"^{re.escape(app_name)}_(\d+)$")
    if os.path.exists(date_dir):
        for entry in os.listdir(date_dir):
            if os.path.isdir(os.path.join(date_dir, entry)):
                match = pattern.match(entry)
                if match:
                    try:
                        count = int(match.group(1))
                        if count > max_count:
                            max_count = count
                    except ValueError:
                        pass
    
    next_count = max_count + 1
    run_folder_name = f"{app_name}_{next_count:02d}"
    return os.path.join(date_dir, run_folder_name)

def run_perception_agent(user_instruction: str, output_dir: str = "output", headed: bool = False):
    """
    Perception-agent entry point.

    A plain URL keeps the original single-page perception flow.
    A natural-language instruction is handed to the execution agent, which asks
    perception to recapture pages whenever navigation or uncertainty occurs.
    """
    if looks_like_plain_url(user_instruction):
        app_name = extract_app_name(user_instruction)
        run_dir = get_next_run_dir(output_dir, app_name)
        main_scraper(user_instruction, run_dir)
        return {
            "mode": "perception_only",
            "output_dir": run_dir
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
