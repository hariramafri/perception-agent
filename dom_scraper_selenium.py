import os
import json
import time
import argparse
import re
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
from telemetry import TelemetryAgent

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

def scrape_dom(url: str, output_dir: str = "output"):
    """
    Navigates to a URL, takes screenshots, and extracts interactive element details using Selenium.
    """
    os.makedirs(output_dir, exist_ok=True)
    
    # Setup Chrome options for headless mode
    options = Options()
    options.add_argument('--headless')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--disable-gpu')
    
    print("Setting up Chrome WebDriver...")
    # This automatically downloads the correct ChromeDriver for your system
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    
    telemetry = TelemetryAgent(output_dir)
    telemetry.set_driver(driver)
    
    try:
        with telemetry.track_action("URL Navigation"):
            print(f"Navigating to {url}...")
            driver.get(url)
            # Give the page a moment to fully render and execute any initial JS
            time.sleep(3) 
        
        with telemetry.track_action("Screenshot Capture"):
            # Take full page screenshot
            full_screenshot_path = os.path.join(output_dir, "full_page.png")
            driver.save_screenshot(full_screenshot_path)
            print(f"Full page screenshot saved to {full_screenshot_path}")
        
        with telemetry.track_action("DOM Extraction"):
            # Select interactive elements via CSS
            selectors = "button, input, a, select, textarea, [role='button'], [tabindex]:not([tabindex='-1'])"
            elements = driver.find_elements(By.CSS_SELECTOR, selectors)
            
            print(f"Found {len(elements)} potentially interactive elements. Extracting data...")
            extracted_elements = []
            used_names = set()
            
            for i, element in enumerate(elements):
                try:
                    # Check if element is displayed (visible)
                    if not element.is_displayed():
                        continue
                        
                    # Get bounding box (size and location)
                    size = element.size
                    location = element.location
                    
                    # Skip elements with no physical dimensions
                    if size['width'] == 0 or size['height'] == 0:
                        continue
                        
                    # Extract HTML properties using JavaScript execution
                    props = driver.execute_script('''
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
                
                        function getCssPath(el) {
                            if (!(el instanceof Element)) return '';
                            var path = [];
                            while (el.nodeType === Node.ELEMENT_NODE) {
                                var selector = el.nodeName.toLowerCase();
                                if (el.id) {
                                    selector += '#' + el.id;
                                    path.unshift(selector);
                                    break;
                                } else {
                                    var sib = el, nth = 1;
                                    while (sib = sib.previousElementSibling) {
                                        if (sib.nodeName.toLowerCase() == selector)
                                           nth++;
                                    }
                                    if (nth != 1) selector += ":nth-of-type("+nth+")";
                                }
                                path.unshift(selector);
                                el = el.parentNode;
                            }
                            return path.join(" > ");
                        }
                        
                        var el = arguments[0];
                        if (!isElementVisible(el)) return null;
                        
                        return {
                            tagName: el.tagName.toLowerCase(),
                            innerText: el.innerText ? el.innerText.trim() : '',
                            id: el.id || '',
                            className: typeof el.className === 'string' ? el.className : (el.getAttribute('class') || ''),
                            name: el.name || el.getAttribute('name') || '',
                            ariaLabel: el.getAttribute('aria-label') || '',
                            contextHint: getContext(el),
                            absXPath: getAbsXPath(el),
                            cssPath: getCssPath(el)
                        };
                    ''', element)
                    
                    if not props:
                        continue
                    
                    tag_name = props["tagName"]
                    inner_text = props["innerText"]
                    aria_label = props["ariaLabel"]
                    context_hint = props.get("contextHint", "")
                    base_name = build_semantic_name({
                        **props,
                        "x": location["x"],
                        "y": location["y"]
                    })
                    
                    semantic_name = base_name
                    if semantic_name in used_names:
                        semantic_name = f"{base_name}_order_{i}"
                    
                    semantic_name = semantic_name[:50].rstrip('_')
                    
                    # Hard collision check if it's still duplicated due to truncation
                    collision_counter = 1
                    while semantic_name in used_names:
                        suffix = f"_{collision_counter}"
                        semantic_name = semantic_name[:50 - len(suffix)].rstrip('_') + suffix
                        collision_counter += 1
                        
                    used_names.add(semantic_name)
                    
                    extracted_elements.append({
                        "source_index": i,
                        "element": element,
                        "semantic_name": semantic_name,
                        "tag_name": tag_name,
                        "inner_text": inner_text,
                        "attributes": {
                            "id": props["id"],
                            "class": props["className"],
                            "name": props["name"],
                            "aria-label": aria_label
                        },
                        "abs_xpath": props.get("absXPath", ""),
                        "css_path": props.get("cssPath", ""),
                        "bounding_box": {
                            "x": location["x"],
                            "y": location["y"],
                            "width": size["width"],
                            "height": size["height"]
                        }
                    })
                    
                except Exception as e:
                    # Some elements might become stale, detached, or throw errors
                    continue

            extracted_elements.sort(
                key=lambda item: (
                    round(float(item["bounding_box"].get("y", 0)) / 8) * 8,
                    float(item["bounding_box"].get("x", 0))
                )
            )

            observations = []
            for page_order, item in enumerate(extracted_elements, start=1):
                element = item.pop("element")
                source_index = item.pop("source_index")
                semantic_name = item["semantic_name"]
                with telemetry.track_action(f"Element {source_index} Screenshot Capture"):
                    element_img_path = os.path.join(output_dir, f"{page_order:04d}_{semantic_name}.png")
                    element.screenshot(element_img_path)

                item["element_index"] = page_order - 1
                item["page_order"] = page_order
                item["screenshot_path"] = element_img_path
                observations.append(item)
                
        # Output structured JSON file
        json_report_path = os.path.join(output_dir, "dom_observation.json")
        with open(json_report_path, "w", encoding="utf-8") as f:
            json.dump({
                "url": url,
                "full_page_screenshot": full_screenshot_path,
                "total_interactive_elements": len(observations),
                "elements": observations
            }, f, indent=4)
            
        print(f"\nExtraction complete!")
        print(f"Processed {len(observations)} visible interactive elements.")
        print(f"Saved DOM observation report to {json_report_path}")
        
    finally:
        driver.quit()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="High-fidelity DOM Scraper using Selenium")
    parser.add_argument("url", help="The URL to scrape (e.g., https://example.com)")
    parser.add_argument("--output", "-o", default="output", help="Output directory for screenshots and JSON (default: output)")
    
    args = parser.parse_args()
    scrape_dom(args.url, args.output)
