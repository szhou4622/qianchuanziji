// Escape spreadsheet formulas before applying CSV structural quoting.
export function csvCell(value: unknown) {
  const text = String(value ?? "");
  const safe = typeof value === "string" && /^[\s\uFEFF]*[=+@\-\t\r\n]/u.test(text)
    ? "'" + text : text;
  return `"${safe.replaceAll('"', '""')}"`;
}
