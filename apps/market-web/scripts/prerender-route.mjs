export function normalizePrerenderPath(route) {
  return String(route || "/").replace(/\/+$/, "") || "/";
}

export function markPrerenderPath(html, route) {
  if (!/<html\b[^>]*>/i.test(html)) {
    throw new Error("Prerendered document is missing an html element");
  }

  const escapedRoute = normalizePrerenderPath(route)
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return html.replace(/<html\b([^>]*)>/i, (_match, attributes) => {
    const cleanAttributes = String(attributes).replace(
      /\sdata-prerender-path=(?:"[^"]*"|'[^']*')/i,
      "",
    );
    return `<html${cleanAttributes} data-prerender-path="${escapedRoute}">`;
  });
}
