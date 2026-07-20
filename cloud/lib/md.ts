/** Tiny Markdown subset -> HTML (port of report_html._md_to_html). */

const esc = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

const inline = (s: string) =>
  esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`(.+?)`/g, "<code>$1</code>");

export function mdToHtml(md: string): string {
  const out: string[] = [];
  let listTag: "ul" | "ol" | null = null;
  const para: string[] = [];

  const closePara = () => {
    if (para.length) {
      out.push(`<p>${inline(para.join(" "))}</p>`);
      para.length = 0;
    }
  };
  const closeList = () => {
    if (listTag) {
      out.push(`</${listTag}>`);
      listTag = null;
    }
  };
  const openList = (tag: "ul" | "ol", start = 1) => {
    if (listTag !== tag) {
      closeList();
      out.push(tag === "ol" && start !== 1 ? `<ol start="${start}">` : `<${tag}>`);
      listTag = tag;
    }
  };

  for (const raw of md.split("\n")) {
    const line = raw.trimEnd();
    const stripped = line.trim();
    if (!stripped) {
      closePara();
      closeList();
      continue;
    }
    const ord = /^(\d+)\.\s+/.exec(stripped);
    if (line.startsWith("### ")) { closePara(); closeList(); out.push(`<h3>${esc(line.slice(4))}</h3>`); }
    else if (line.startsWith("## ")) { closePara(); closeList(); out.push(`<h2>${esc(line.slice(3))}</h2>`); }
    else if (line.startsWith("# ")) { closePara(); closeList(); out.push(`<h1>${esc(line.slice(2))}</h1>`); }
    else if (stripped === "---") { closePara(); closeList(); out.push("<hr>"); }
    else if (/^[-*]\s+/.test(stripped)) { closePara(); openList("ul"); out.push(`<li>${inline(stripped.replace(/^[-*]\s+/, ""))}</li>`); }
    else if (ord) { closePara(); openList("ol", parseInt(ord[1], 10)); out.push(`<li>${inline(stripped.slice(ord[0].length))}</li>`); }
    else if (listTag) { out[out.length - 1] = out[out.length - 1].slice(0, -5) + " " + inline(stripped) + "</li>"; }
    else para.push(stripped);
  }
  closePara();
  closeList();
  return out.join("\n");
}
