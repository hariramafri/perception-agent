import ast

with open('dom_scraper.py', 'r', encoding='utf-8') as f:
    code = f.read()

tree = ast.parse(code)
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == 'js_script':
                if isinstance(node.value, ast.Constant):
                    js_code = node.value.value
                    with open('scratch.js', 'w', encoding='utf-8') as out:
                        out.write(js_code)
                    print("Extracted to scratch.js")
