import type { HelpContent, HelpSection } from "@/types";
import helpMarkdown from "./help.md?raw";

function parseMd(markdown: string): HelpContent {
  const lines = markdown.split("\n");
  let title = "";
  const sections: HelpSection[] = [];
  let currentSection: HelpSection | null = null;

  for (const line of lines) {
    if (line.startsWith("# ")) {
      title = line.slice(2).trim();
    } else if (line.startsWith("## ")) {
      if (currentSection) {
        sections.push({ ...currentSection, content: currentSection.content.trim() });
      }
      currentSection = {
        title: line.slice(3).trim(),
        content: "",
      };
    } else if (currentSection && line.trim()) {
      currentSection.content += line + " ";
    }
  }

  if (currentSection) {
    sections.push({ ...currentSection, content: currentSection.content.trim() });
  }

  return { title, sections };
}

export const helpContent = parseMd(helpMarkdown);
