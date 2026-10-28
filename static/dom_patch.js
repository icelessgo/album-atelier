// Keyed DOM reconciliation keeps image elements, scroll positions and keyboard
// focus alive while the album changes. No production framework is required.
function key(node) {
  return node.nodeType === 1 && (node.getAttribute('data-key') || node.id) || null;
}
function compatible(current, next) {
  return current.nodeType === next.nodeType && current.nodeName === next.nodeName && key(current) === key(next);
}
function patchNode(current, next) {
  if (current.nodeType !== 1) {
    if (current.nodeValue !== next.nodeValue) current.nodeValue = next.nodeValue;
    return;
  }
  for (const attribute of [...current.attributes]) {
    if (!next.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
  }
  for (const attribute of [...next.attributes]) {
    if (current.getAttribute(attribute.name) !== attribute.value) {
      current.setAttribute(attribute.name, attribute.value);
      if (attribute.name === 'value' && 'value' in current) current.value = attribute.value;
      if (attribute.name === 'checked') current.checked = true;
    }
  }
  if (current.tagName === 'INPUT' && !next.hasAttribute('checked') && current.type === 'checkbox') current.checked = false;
  patchChildren(current, next);
}
export function patchChildren(current, next) {
  const desiredKeys = new Set([...next.childNodes].map(key).filter(Boolean));
  // Remove obsolete keyed children before matching their following siblings.
  // Otherwise replacing a spread could unnecessarily recreate its controls.
  for (const node of [...current.childNodes]) {
    if (key(node) && !desiredKeys.has(key(node))) node.remove();
  }
  const keyed = new Map([...current.childNodes].filter(key).map(node => [key(node), node]));
  let cursor = current.firstChild;
  for (const target of [...next.childNodes]) {
    let node = key(target) ? keyed.get(key(target)) : cursor;
    if (!node || !compatible(node, target)) {
      node = target.cloneNode(true);
      if (cursor && !key(cursor) && !key(target)) cursor.replaceWith(node);
      else current.insertBefore(node, cursor);
    } else {
      if (node !== cursor) current.insertBefore(node, cursor);
      patchNode(node, target);
    }
    cursor = node.nextSibling;
  }
  while (cursor) { const nextNode = cursor.nextSibling; cursor.remove(); cursor = nextNode; }
}
export function patchHtml(root, html) {
  const template = root.ownerDocument.createElement('template');
  template.innerHTML = html;
  patchChildren(root, template.content);
}
