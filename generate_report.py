import json
import csv
import os
import argparse
import re
from telemetry import TelemetryAgent

def is_stable_attribute(value):
    """Filters out dynamic React/Vue classes, CSS-in-JS hashes, UUIDs, timestamps."""
    if not value: return False
    value_str = str(value)
    
    # UUIDs
    if re.search(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', value_str, re.IGNORECASE): return False
    
    # CSS-in-JS and Framework generated classes
    if re.search(r'(mui-[a-zA-Z0-9]+|css-[a-zA-Z0-9]+|react-[a-zA-Z0-9]+)', value_str, re.IGNORECASE): return False
    
    # JSS classes
    if re.search(r'jss\d+', value_str, re.IGNORECASE): return False
    
    # Hashes at the end of classes (e.g. btn-a3f9b2)
    if re.search(r'-[a-f0-9]{5,}$', value_str, re.IGNORECASE): return False
    
    # Long numbers / timestamps
    if re.search(r'\d{10,}', value_str): return False
    
    return True

def determine_element_type(el):
    tag = el.get("tag_name", "").lower()
    if tag == "a": return "Link"
    if tag == "button": return "Button"
    if tag == "input": return "Input Field"
    if tag == "select": return "Dropdown Menu"
    if tag == "textarea": return "Text Area"
    return "Interactive Element"

def determine_logical_name(el):
    text = el.get("inner_text", "").strip()
    context = el.get("context_hint", "")
    if el.get("bounding_box", {}).get("y", 0) < 120:
        context = ""
    if "International Shopping Transition Alert" in context:
        context = ""
    if text: 
        if context and context.lower() not in text.lower():
            return f"{text} - {context}"
        return text
    attrs = el.get("attributes", {})
    if attrs.get("aria-label"): 
        if context and context.lower() not in attrs["aria-label"].lower():
            return f"{attrs['aria-label']} - {context}"
        return attrs["aria-label"]
    if attrs.get("name"): return attrs["name"]
    if attrs.get("id"): return attrs["id"]
    return "Unknown Context"

def generate_all_locators(el):
    tag = el.get("tag_name", "div").lower()
    attrs = el.get("attributes", {})
    text = el.get("inner_text", "").strip()
    
    # 1. Playwright Best
    pw_best = "-"
    if attrs.get("data-testid") and is_stable_attribute(attrs["data-testid"]):
        pw_best = f"await page.getByTestId('{attrs['data-testid']}')"
    elif attrs.get("aria-label") and is_stable_attribute(attrs["aria-label"]):
        pw_best = f"await page.getByLabel('{attrs['aria-label']}')"
    elif text and len(text) < 50:
        clean_text = text.replace("'", "\\'")
        if tag == "a":
            pw_best = f"await page.getByRole('link', name='{clean_text}')"
        elif tag == "button":
            pw_best = f"await page.getByRole('button', name='{clean_text}')"
        else:
            pw_best = f"await page.getByText('{clean_text}')"
    elif attrs.get("id") and is_stable_attribute(attrs["id"]):
        pw_best = f"await page.locator('#{attrs['id']}')"
    elif attrs.get("name") and is_stable_attribute(attrs["name"]):
        pw_best = f"await page.locator('[name=\"{attrs['name']}\"]')"
    else:
        css_path = el.get('css_path', '')
        if css_path:
            pw_best = f"await page.locator('{css_path}')"
        else:
            pw_best = f"await page.locator('{tag}')"

    # 2. Relative XPath
    rel_xpath = el.get("rel_xpath", "-")
    if not rel_xpath or rel_xpath == "-":
        if attrs.get("id") and is_stable_attribute(attrs["id"]):
            rel_xpath = f"//{tag}[@id='{attrs['id']}']"
        elif attrs.get("data-testid") and is_stable_attribute(attrs["data-testid"]):
            rel_xpath = f"//{tag}[@data-testid='{attrs['data-testid']}']"
        elif attrs.get("name") and is_stable_attribute(attrs["name"]):
            rel_xpath = f"//{tag}[@name='{attrs['name']}']"
        elif text and len(text) < 50:
            clean_text = text.replace("'", "\\'")
            rel_xpath = f"//{tag}[normalize-space()='{clean_text}']"
        elif attrs.get("class"):
            classes = [c for c in attrs["class"].split() if is_stable_attribute(c)]
            if classes:
                rel_xpath = f"//{tag}[contains(@class, '{classes[0]}')]"
            
    # 3. CSS Selector
    css_sel = el.get("css_path", "")
    if not css_sel:
        if attrs.get("id") and is_stable_attribute(attrs["id"]):
            css_sel = f"#{attrs['id']}"
        elif attrs.get("data-testid") and is_stable_attribute(attrs["data-testid"]):
            css_sel = f"{tag}[data-testid='{attrs['data-testid']}']"
        
    return {
        "pw_best": pw_best,
        "rel_xpath": rel_xpath,
        "abs_xpath": el.get("abs_xpath", ""),
        "css_sel": css_sel,
        "id": attrs.get("id", ""),
        "name": attrs.get("name", ""),
        "class": attrs.get("class", "")
    }

def calculate_confidence(locators_dict):
    pw = locators_dict.get("pw_best", "")
    if "getByTestId" in pw: return 100
    if "getByLabel" in pw: return 95
    if "getByRole" in pw: return 90
    if "getByText" in pw: return 85
    if "#" in pw: return 80
    if "name=" in pw: return 75
    return 50

def sort_elements_by_page_order(elements):
    def page_sort_key(el):
        bbox = el.get("bounding_box", {})
        if el.get("page_order") is not None:
            return (0, int(el.get("page_order", 0)), 0, 0)
        return (
            1,
            round(float(bbox.get("y", 0)) / 8) * 8,
            float(bbox.get("x", 0)),
            int(el.get("element_index", 0))
        )

    return sorted(elements, key=page_sort_key)

def build_report_rows(data):
    elements = data.get("elements", [])
    if not elements:
        return []

    elements = sort_elements_by_page_order(elements)
    rows = []

    for el in elements:
        el["raw_locators"] = generate_all_locators(el)

    for i, el in enumerate(elements):
        locs = el["raw_locators"]
        pw_str = locs["pw_best"]
        pw_final = pw_str
        if ".nth(" not in pw_str and "locator" not in pw_str and pw_str != "-":
            matches = [e for e in elements if e.get("raw_locators", {}).get("pw_best") == pw_str]
            if len(matches) > 1:
                if locs.get("css_sel") and ":nth-of-type" not in locs.get("css_sel"):
                    pw_final = f"await page.locator(\"{locs['css_sel']}\")"
                else:
                    idx = matches.index(el)
                    pw_final = f"{pw_str}.nth({idx})"

        xpath_str = locs["rel_xpath"]
        xpath_final = xpath_str
        if xpath_str != "-":
            matches = [e for e in elements if e.get("raw_locators", {}).get("rel_xpath") == xpath_str]
            if len(matches) > 1:
                idx = matches.index(el) + 1
                xpath_final = f"({xpath_str})[{idx}]"

        bbox = el.get("bounding_box", {})
        rows.append({
            "Page Order": el.get("page_order", i + 1),
            "Semantic Name": el.get("semantic_name", f"element_{el.get('element_index', i)}"),
            "Element Type": determine_element_type(el),
            "Logical Name": determine_logical_name(el),
            "Playwright Best": pw_final,
            "Relative XPath": xpath_final,
            "Absolute XPath": locs["abs_xpath"],
            "CSS Selector": locs["css_sel"],
            "ID Attribute": locs["id"],
            "Name Attribute": locs["name"],
            "Class Name": locs["class"],
            "Coordinates": f"({bbox.get('x', 0)}, {bbox.get('y', 0)})",
            "Dimension": f"{bbox.get('width', 0)} Ã— {bbox.get('height', 0)}",
            "Confidence Score": f"{calculate_confidence(locs)}"
        })

    return rows

def generate_report(input_json, output_dir):
    """Parses observation JSON and generates CSV and Markdown reports."""
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        with open(input_json, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Error: Could not find {input_json}. Please run the scraper first.")
        return
        
    elements = data.get("elements", [])
    if not elements:
        print("No elements found in JSON data.")
        return

    def page_sort_key(el):
        bbox = el.get("bounding_box", {})
        if el.get("page_order") is not None:
            return (0, int(el.get("page_order", 0)), 0, 0)
        return (
            1,
            round(float(bbox.get("y", 0)) / 8) * 8,
            float(bbox.get("x", 0)),
            int(el.get("element_index", 0))
        )

    elements = sorted(elements, key=page_sort_key)
        
    rows = []
    telemetry = TelemetryAgent(output_dir)
    
    try:
        with telemetry.track_action("Locator generation"):
            # Pass 1: Generate locators
            for el in elements:
                el["raw_locators"] = generate_all_locators(el)
                
            # Pass 2: Enforce uniqueness
            for i, el in enumerate(elements):
                locs = el["raw_locators"]
                
                # Playwright uniqueness
                pw_str = locs["pw_best"]
                pw_final = pw_str
                if ".nth(" not in pw_str and "locator" not in pw_str and pw_str != "-":
                    matches = [e for e in elements if e.get("raw_locators", {}).get("pw_best") == pw_str]
                    if len(matches) > 1:
                        if locs.get("css_sel") and ":nth-of-type" not in locs.get("css_sel"):
                            pw_final = f"await page.locator(\"{locs['css_sel']}\")"
                        else:
                            idx = matches.index(el)
                            pw_final = f"{pw_str}.nth({idx})"
                        
                # XPath uniqueness
                xpath_str = locs["rel_xpath"]
                xpath_final = xpath_str
                if xpath_str != "-":
                    matches = [e for e in elements if e.get("raw_locators", {}).get("rel_xpath") == xpath_str]
                    if len(matches) > 1:
                        idx = matches.index(el) + 1
                        xpath_final = f"({xpath_str})[{idx}]"
                        
                el_type = determine_element_type(el)
                logical_name = determine_logical_name(el)
                semantic_name = el.get("semantic_name", f"element_{el.get('element_index', i)}")
                
                confidence = calculate_confidence(locs)
                
                bbox = el.get("bounding_box", {})
                coords = f"({bbox.get('x', 0)}, {bbox.get('y', 0)})"
                dims = f"{bbox.get('width', 0)} × {bbox.get('height', 0)}"
                
                row_data = {
                    "Page Order": el.get("page_order", i + 1),
                    "Semantic Name": semantic_name,
                    "Element Type": el_type,
                    "Logical Name": logical_name,
                    "Playwright Best": pw_final,
                    "Relative XPath": xpath_final,
                    "Absolute XPath": locs["abs_xpath"],
                    "CSS Selector": locs["css_sel"],
                    "ID Attribute": locs["id"],
                    "Name Attribute": locs["name"],
                    "Class Name": locs["class"],
                    "Coordinates": coords,
                    "Dimension": dims,
                    "Confidence Score": f"{confidence}"
                }
                rows.append(row_data)
                
    except Exception as e:
        print(f"Failed to generate locators: {e}")
        return
        
    # Write CSV
    csv_path = os.path.join(output_dir, "perception_report.csv")
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
        
    # Write MD
    md_path = os.path.join(output_dir, "perception_report.md")
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write("# Perception Agent - Semantic Element Report\n\n")
        f.write(f"**Target URL:** `{data.get('url', 'Unknown')}`\n")
        f.write(f"**Total Analyzed Elements:** {len(rows)}\n\n")
        
        headers = list(rows[0].keys())
        f.write("| " + " | ".join(headers) + " |\n")
        f.write("|" + "|".join(["---"] * len(headers)) + "|\n")
        
        for row in rows:
            f_row = [str(row[h]).replace("|", "\\|").replace("\n", " ") for h in headers]
            f.write("| " + " | ".join(f_row) + " |\n")
            
    print(f"\nReport Generation Complete!")
    print(f"Data saved to: '{output_dir}'")
    print(f"  - [CSV]  {csv_path} (Optimized for Automation tools)")
    print(f"  - [MD]   {md_path} (Optimized for Human viewing)")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate Perception Agent Final Report")
    parser.add_argument("--input", "-i", default="output/dom_observation.json", help="Path to input JSON")
    parser.add_argument("--output", "-o", default="output", help="Directory for output reports")
    
    args = parser.parse_args()
    generate_report(args.input, args.output)
