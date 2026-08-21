import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";


const [contractPath, outputPath, qaDirectory, nodeModules] = process.argv.slice(2);
if (!contractPath || !outputPath || !qaDirectory || !nodeModules) {
  throw new Error(
    "usage: render_workbook.mjs CONTRACT OUTPUT_XLSX QA_DIRECTORY NODE_MODULES",
  );
}

const requireFromRuntime = createRequire(
  path.join(path.resolve(nodeModules), "artifact-entry.cjs"),
);
const artifactEntry = requireFromRuntime.resolve("@oai/artifact-tool");
const { SpreadsheetFile, Workbook } = await import(artifactEntry);
const contract = JSON.parse(await fs.readFile(contractPath, "utf8"));
if (contract.experiment_schema !== 2 || !Array.isArray(contract.sheets)) {
  throw new Error("invalid Schema-2 workbook contract");
}

function columnName(index) {
  let value = index + 1;
  let result = "";
  while (value > 0) {
    value -= 1;
    result = String.fromCharCode(65 + (value % 26)) + result;
    value = Math.floor(value / 26);
  }
  return result;
}

function scalar(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function numberFormat(column) {
  const name = column.toLowerCase();
  if (name.includes("sha256") || name.includes("path") || name === "error") return "@";
  if (name.includes("rate") || name.includes("fidelity") || name.includes("ratio") ||
      name.includes("p_value") || name.includes("wilson") || name.includes("ci95") ||
      name.includes("rank_biserial")) return "0.000000";
  if (name.includes("seconds") || name.includes("time_us") || name.includes("duration_us")) {
    return "0.000";
  }
  if (name === "n" || name === "valid" || name.includes("count") ||
      name.includes("batches") || name.includes("attempts") || name.includes("successful") ||
      name.includes("hits") || name.includes("repairs") || name.includes("splits") ||
      name.includes("bytes") || name === "seed" || name === "repetition") return "0";
  return "General";
}

function safeFileName(name) {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "sheet";
}

await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.mkdir(qaDirectory, { recursive: true });
const workbook = Workbook.create();
const previews = [];
const formulaErrors = [];

for (let sheetIndex = 0; sheetIndex < contract.sheets.length; sheetIndex += 1) {
  const spec = contract.sheets[sheetIndex];
  const columns = spec.columns;
  const rows = spec.rows;
  if (!Array.isArray(columns) || columns.length === 0 || !Array.isArray(rows)) {
    throw new Error(`invalid sheet contract: ${spec.sheet_name}`);
  }
  const sheet = workbook.worksheets.add(spec.sheet_name);
  sheet.showGridLines = false;
  const matrix = [
    columns,
    ...rows.map((row) => columns.map((column) => scalar(row[column]))),
  ];
  const lastColumn = columnName(columns.length - 1);
  const lastRow = matrix.length;
  const used = sheet.getRange(`A1:${lastColumn}${lastRow}`);
  used.values = matrix;
  used.format = {
    font: { name: "Aptos", size: 10, color: "#1F2937" },
    verticalAlignment: "center",
    borders: { preset: "all", style: "thin", color: "#D9E2F3" },
  };
  const header = sheet.getRange(`A1:${lastColumn}1`);
  header.format = {
    fill: "#17365D",
    font: { name: "Aptos Display", size: 10, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: "#17365D" },
    rowHeight: 30,
  };
  if (rows.length > 0) {
    const table = sheet.tables.add(`A1:${lastColumn}${lastRow}`, true,
      `T${String(sheetIndex + 1).padStart(2, "0")}${safeFileName(spec.sheet_name).replaceAll("-", "").slice(0, 18)}`);
    table.style = "TableStyleMedium2";
    table.showFilterButton = spec.auto_filter !== false;
    table.showBandedColumns = false;
  }
  sheet.freezePanes.freezeRows(1);
  for (let columnIndex = 0; columnIndex < columns.length; columnIndex += 1) {
    const letter = columnName(columnIndex);
    const values = matrix.map((row) => row[columnIndex]);
    const maximumLength = Math.max(
      columns[columnIndex].length,
      ...values.slice(1).map((value) => value === null ? 0 : String(value).length),
    );
    const columnRange = sheet.getRange(`${letter}1:${letter}${lastRow}`);
    columnRange.format.columnWidth = Math.min(42, Math.max(9, maximumLength + 2));
    if (lastRow > 1) {
      sheet.getRange(`${letter}2:${letter}${lastRow}`).format.numberFormat =
        numberFormat(columns[columnIndex]);
    }
  }
  if (lastRow > 1) {
    sheet.getRange(`A2:${lastColumn}${lastRow}`).format.wrapText = false;
  }
  const formulaInspection = await workbook.inspect({
    kind: "formula", sheetId: spec.sheet_name,
    range: `A1:${lastColumn}${Math.min(lastRow, 100)}`,
    maxChars: 4000, options: { maxResults: 200 },
  });
  const formulaText = formulaInspection.ndjson || "";
  const matches = formulaText.match(/#(?:REF!|DIV\/0!|VALUE!|NAME\?|N\/A|NUM!|NULL!)/g) || [];
  formulaErrors.push(...matches.map((error) => ({ sheet: spec.sheet_name, error })));
  const previewName = `${String(sheetIndex + 1).padStart(2, "0")}-${safeFileName(spec.sheet_name)}.png`;
  const preview = await workbook.render({
    sheetName: spec.sheet_name,
    range: `A1:${lastColumn}${Math.min(lastRow, 35)}`,
    scale: 0.8,
    headers: true,
    format: "png",
  });
  await fs.writeFile(
    path.join(qaDirectory, previewName),
    new Uint8Array(await preview.arrayBuffer()),
  );
  previews.push(previewName);
}

const inspection = await workbook.inspect({
  kind: "workbook,sheet,table",
  maxChars: 12000,
  tableMaxRows: 4,
  tableMaxCols: 10,
  tableMaxCellChars: 100,
});
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);

let artifactToolVersion = "unknown";
try {
  const packageJson = JSON.parse(
    await fs.readFile(path.join(nodeModules, "@oai", "artifact-tool", "package.json"), "utf8"),
  );
  artifactToolVersion = packageJson.version || "unknown";
} catch {
  // Version is informative only; the rendered workbook and QA remain authoritative.
}

await fs.writeFile(
  path.join(qaDirectory, "workbook_qa.json"),
  `${JSON.stringify({
    experiment_schema: 2,
    dataset: contract.dataset,
    artifact_tool_version: artifactToolVersion,
    sheets: contract.sheets.map((sheet) => sheet.sheet_name),
    previews,
    formula_errors: formulaErrors,
    inspection_ndjson: inspection.ndjson || "",
  }, null, 2)}\n`,
  "utf8",
);
