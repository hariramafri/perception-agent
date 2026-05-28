
        function getContext(el) {
            let parent = el.parentElement;
            for (let i = 0; i < 5; i++) {
                if (!parent || parent.tagName === 'BODY') break;
                let h = parent.querySelector('h1, h2, h3, h4, h5, h6, [class*="title"], [class*="name"], [class*="product"]');
                if (h && h !== el && !el.contains(h) && h.innerText && h.innerText.trim()) {
                    let text = h.innerText.trim();
                    if (text.length > 0 && text.length < 60) return text;
                }
                parent = parent.parentElement;
            }
            parent = el.parentElement;
            for (let i = 0; i < 4; i++) {
                if (!parent || parent.tagName === 'BODY') break;
                let text = parent.innerText ? parent.innerText.trim().split('
')[0] : '';
                if (text && text !== el.innerText.trim() && text.length > 0 && text.length < 60) {
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
            if (str.match(/-\w{5,}$/)) return false;
            if (str.match(/\d{5,}/)) return false;
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
                    var classes = className.split(/\s+/).filter(isStable);
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
                        var classes = className.split(/\s+/).filter(isStable);
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
        