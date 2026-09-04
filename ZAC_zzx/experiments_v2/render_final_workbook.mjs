import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";


const [contractPathArg, outputPathArg, qaDirectoryArg] = process.argv.slice(2);
if (!contractPathArg || !outputPathArg || !qaDirectoryArg) {
  throw new Error(
    "usage: render_final_workbook.mjs CONTRACT OUTPUT_XLSX QA_DIRECTORY",
  );
}

const contractPath = path.resolve(contractPathArg);
const outputPath = path.resolve(outputPathArg);
const qaDirectory = path.resolve(qaDirectoryArg);
const contractDirectory = path.dirname(contractPath);
const contract = JSON.parse(await fs.readFile(contractPath, "utf8"));
const requiredSheets = ["ZAC18", "QMAP154"];
const METHODS_FOR_SUMMARY = ["M1", "M2", "M3", "M4"];

if (
  contract.experiment_schema !== 2 ||
  contract.contract_id !== "native-ga-v1-two-sheet-results-v1" ||
  contract.exact_sheet_count !== 2 ||
  JSON.stringify(contract.sheet_names) !== JSON.stringify(requiredSheets) ||
  contract.charts !== false ||
  !Array.isArray(contract.sheets) ||
  contract.sheets.length !== 2
) {
  throw new Error("invalid final two-sheet workbook contract");
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

function asTypedValue(value, column) {
  if (value === null || value === undefined || value === "") return null;
  if (column.type === "number") {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) {
      throw new Error(`non-finite numeric value for ${column.key}: ${value}`);
    }
    return numeric;
  }
  if (column.type === "boolean") {
    if (value === true || String(value).toLowerCase() === "true") return true;
    if (value === false || String(value).toLowerCase() === "false") return false;
    throw new Error(`invalid boolean value for ${column.key}: ${value}`);
  }
  return String(value);
}

function safeFileName(name) {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function contiguousHeaderGroups(columns) {
  const groups = [];
  let start = 0;
  while (start < columns.length) {
    const group = columns[start].group;
    let end = start;
    while (end + 1 < columns.length && columns[end + 1].group === group) end += 1;
    groups.push({ group, start, end });
    start = end + 1;
  }
  return groups;
}

function validateFrozenOrder(spec, dataRows, keyIndex) {
  const actual = dataRows.map((row) => keyIndex(row));
  if (JSON.stringify(actual) !== JSON.stringify(spec.frozen_row_order)) {
    throw new Error(`${spec.name} row order differs from the frozen contract`);
  }
}

async function readCsvValues(sourcePath) {
  const csvText = await fs.readFile(sourcePath, "utf8");
  const temporary = await Workbook.fromCSV(csvText, { sheetName: "Imported" });
  const sheet = temporary.worksheets.getItem("Imported");
  const used = sheet.getUsedRange(true);
  if (!used) throw new Error(`empty CSV source: ${sourcePath}`);
  return used.values;
}

await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.mkdir(qaDirectory, { recursive: true });
const workbook = Workbook.create();
const qa = {
  experiment_schema: 2,
  contract_id: contract.contract_id,
  sheets: [],
  formula_errors: [],
  inspections: {},
};

const groupStyles = {
  M1: { fill: "#4472C4", font: "#FFFFFF" },
  M2: { fill: "#ED7D31", font: "#FFFFFF" },
  M3: { fill: "#70AD47", font: "#FFFFFF" },
  M4: { fill: "#8064A2", font: "#FFFFFF" },
  "同阶段比较": { fill: "#5B5F72", font: "#FFFFFF" },
};

for (let sheetIndex = 0; sheetIndex < contract.sheets.length; sheetIndex += 1) {
  const spec = contract.sheets[sheetIndex];
  if (spec.name !== requiredSheets[sheetIndex] || spec.header_rows !== 2) {
    throw new Error(`unexpected sheet contract at index ${sheetIndex}`);
  }
  if (!Array.isArray(spec.columns) || spec.columns.length === 0) {
    throw new Error(`missing columns for ${spec.name}`);
  }
  const sourcePath = path.resolve(contractDirectory, spec.source_csv);
  const csvValues = await readCsvValues(sourcePath);
  const expectedKeys = spec.columns.map((column) => column.key);
  if (JSON.stringify(csvValues[0]) !== JSON.stringify(expectedKeys)) {
    throw new Error(`${spec.name} CSV header differs from workbook contract`);
  }
  const dataRows = csvValues.slice(1).map((row) =>
    spec.columns.map((column, columnIndex) => asTypedValue(row[columnIndex], column)),
  );
  validateFrozenOrder(spec, dataRows, (row) => row[0]);
  if (dataRows.length < 2 || dataRows[0][0] !== "整体汇总") {
    throw new Error(`${spec.name} must start with the overall summary row`);
  }
  const overallRow = dataRows[0];
  const detailRows = dataRows.slice(1);
  const columnIndexByKey = new Map(
    spec.columns.map((column, columnIndex) => [column.key, columnIndex]),
  );
  const overallValue = (key) => overallRow[columnIndexByKey.get(key)];
  const commonMatch = String(overallValue("M1__status") || "").match(/=(\d+)$/);
  if (!commonMatch) throw new Error(`${spec.name} lacks the common-cohort count`);
  const commonCount = Number(commonMatch[1]);

  const sheet = workbook.worksheets.add(spec.name);
  sheet.showGridLines = false;
  const columnCount = spec.columns.length;
  const lastColumn = columnName(columnCount - 1);
  const detailGroupRow = 11;
  const detailLabelRow = 12;
  const detailDataStart = 13;
  const lastRow = detailDataStart + detailRows.length - 1;
  const groups = contiguousHeaderGroups(spec.columns);

  const methodLabels = [
    "M1 ZAC",
    "M2 ICCAD/QMAP A*",
    "M3 Ours-NL (H=0)",
    "M4 Ours-LK（衰减前瞻）",
  ];
  const summaryRows = [
    ["指标", ...methodLabels],
    [
      "保真度F（共同有效电路几何均值 ↑）",
      ...METHODS_FOR_SUMMARY.map((method) => overallValue(`${method}__fidelity`)),
    ],
    [
      `共同有效电路数 / ${detailRows.length}`,
      commonCount,
      commonCount,
      commonCount,
      commonCount,
    ],
    [
      "Move批次（共同电路算术均值 ↓）",
      ...METHODS_FOR_SUMMARY.map((method) => overallValue(`${method}__move_batches`)),
    ],
    [
      "Move时间（ms，共同电路算术均值 ↓）",
      ...METHODS_FOR_SUMMARY.map((method) => overallValue(`${method}__move_time_ms`)),
    ],
    [
      "算法时间（s，共同电路算术均值 ↓）",
      ...METHODS_FOR_SUMMARY.map((method) => overallValue(`${method}__algorithm_time_s`)),
    ],
    [
      "成功覆盖 / N",
      ...METHODS_FOR_SUMMARY.map((method) => overallValue(`${method}__valid_over_N`)),
    ],
  ];
  sheet.getRange("A1:E1").merge();
  sheet.getRange("A1").values = [[`${spec.name} 四方法整体对比（seed0）`]];
  sheet.getRange("A2:E8").values = summaryRows;
  sheet.getRange("A9:E9").merge();
  sheet.getRange("A9").values = [[
    "说明：四个整体指标均在四方法共同成功且线性保真度有效的同一电路集合上汇总；保真度取几何均值，其余取算术均值。逐电路结果见下表。",
  ]];

  for (const headerGroup of groups) {
    const firstLetter = columnName(headerGroup.start);
    const lastLetter = columnName(headerGroup.end);
    if (headerGroup.group === null || headerGroup.group === undefined) {
      const range = sheet.getRange(
        `${firstLetter}${detailGroupRow}:${lastLetter}${detailLabelRow}`,
      );
      if (headerGroup.start === headerGroup.end) {
        range.merge();
        range.values = [[spec.columns[headerGroup.start].label]];
      } else {
        for (let index = headerGroup.start; index <= headerGroup.end; index += 1) {
          const letter = columnName(index);
          const cell = sheet.getRange(
            `${letter}${detailGroupRow}:${letter}${detailLabelRow}`,
          );
          cell.merge();
          cell.values = [[spec.columns[index].label]];
        }
      }
    } else {
      const groupRange = sheet.getRange(
        `${firstLetter}${detailGroupRow}:${lastLetter}${detailGroupRow}`,
      );
      groupRange.merge();
      groupRange.values = [[headerGroup.group]];
      sheet.getRange(
        `${firstLetter}${detailLabelRow}:${lastLetter}${detailLabelRow}`,
      ).values = [[
        ...spec.columns.slice(headerGroup.start, headerGroup.end + 1)
          .map((column) => column.label),
      ]];
    }
  }

  if (detailRows.length > 0) {
    sheet.getRange(`A${detailDataStart}:${lastColumn}${lastRow}`).values = detailRows;
  }
  const used = sheet.getRange(`A1:${lastColumn}${lastRow}`);
  used.format = {
    font: { name: "Aptos", size: 10, color: "#1F2937" },
    verticalAlignment: "center",
  };
  sheet.getRange(`A${detailGroupRow}:${lastColumn}${detailLabelRow}`).format = {
    fill: "#E8EEF6",
    font: { name: "Aptos", size: 10, bold: true, color: "#17324D" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: {
      insideHorizontal: { style: "thin", color: "#B8C7D9" },
      insideVertical: { style: "thin", color: "#B8C7D9" },
      bottom: { style: "medium", color: "#7B91A8" },
    },
  };
  sheet.getRange(`A${detailGroupRow}:${lastColumn}${detailGroupRow}`).format.rowHeight = 24;
  sheet.getRange(`A${detailLabelRow}:${lastColumn}${detailLabelRow}`).format.rowHeight = 38;
  for (const headerGroup of groups) {
    if (!headerGroup.group) continue;
    const style = groupStyles[headerGroup.group] || groupStyles["同阶段比较"];
    const range = sheet.getRange(
      `${columnName(headerGroup.start)}${detailGroupRow}:${columnName(headerGroup.end)}${detailGroupRow}`,
    );
    range.format.fill = style.fill;
    range.format.font = {
      name: "Aptos Display", size: 11, bold: true, color: style.font,
    };
  }

  sheet.getRange("A1:E1").format = {
    fill: "#173F66",
    font: { name: "Aptos Display", size: 14, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "left",
    verticalAlignment: "center",
  };
  sheet.getRange("A1:E1").format.rowHeight = 30;
  sheet.getRange("A2:E2").format = {
    fill: "#285A84",
    font: { name: "Aptos Display", size: 11, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: { insideVertical: { style: "thin", color: "#FFFFFF" } },
  };
  const summaryColors = ["#285A84", "#4472C4", "#ED7D31", "#70AD47", "#8064A2"];
  for (let index = 0; index < summaryColors.length; index += 1) {
    sheet.getRange(`${columnName(index)}2:${columnName(index)}2`).format.fill = summaryColors[index];
  }
  sheet.getRange("A3:E8").format = {
    fill: "#EDF3FA",
    font: { name: "Aptos", size: 10, color: "#1F2937" },
    verticalAlignment: "center",
    borders: {
      insideHorizontal: { style: "thin", color: "#B8C7D9" },
      insideVertical: { style: "thin", color: "#B8C7D9" },
      bottom: { style: "thin", color: "#B8C7D9" },
    },
  };
  sheet.getRange("A3:A8").format.font = {
    name: "Aptos", size: 10, bold: true, color: "#17324D",
  };
  sheet.getRange("B3:E3").format.numberFormat = "0.000000E+00";
  sheet.getRange("B4:E4").format.numberFormat = "0";
  sheet.getRange("B5:E5").format.numberFormat = "0.00";
  sheet.getRange("B6:E6").format.numberFormat = "0.000";
  sheet.getRange("B7:E7").format.numberFormat = "0.000000";
  sheet.getRange("B3:E8").format.horizontalAlignment = "right";
  sheet.getRange("A9:E9").format = {
    fill: "#F7F9FC",
    font: { name: "Aptos", size: 9, italic: true, color: "#526273" },
    horizontalAlignment: "left",
    verticalAlignment: "center",
    wrapText: true,
  };
  sheet.getRange("A9:E9").format.rowHeight = 34;

  if (detailRows.length > 0) {
    const body = sheet.getRange(`A${detailDataStart}:${lastColumn}${lastRow}`);
    body.format.borders = {
      insideHorizontal: { style: "thin", color: "#E2E8F0" },
      bottom: { style: "thin", color: "#CBD5E1" },
    };
    body.format.rowHeight = 20;
  }

  for (let columnIndex = 0; columnIndex < columnCount; columnIndex += 1) {
    const column = spec.columns[columnIndex];
    const letter = columnName(columnIndex);
    const range = sheet.getRange(`${letter}${detailDataStart}:${letter}${lastRow}`);
    if (column.type === "number") {
      range.format.numberFormat = column.number_format || "0.000000";
      range.format.horizontalAlignment = "right";
    } else if (column.type === "boolean" || column.key.endsWith("valid_over_N")) {
      range.format.horizontalAlignment = "center";
    } else {
      range.format.horizontalAlignment = "left";
    }
    let width = 14;
    if (column.key === "circuit") width = 28;
    else if (column.key === "dataset") width = 12;
    else if (column.key === "layer_ledger_reason") width = 32;
    else if (column.key.endsWith("valid_over_N")) width = 10;
    else if (column.label.length > 16) width = 18;
    sheet.getRange(`${letter}${detailGroupRow}:${letter}${lastRow}`).format.columnWidth = width;
  }
  sheet.getRange("A1:A9").format.columnWidth = 38;
  sheet.getRange("B1:E9").format.columnWidth = 22;
  sheet.freezePanes.freezeRows(detailLabelRow);
  sheet.freezePanes.freezeColumns(spec.freeze_columns || 1);

  const keyRange = `A1:${lastColumn}${Math.min(lastRow, 24)}`;
  const inspection = await workbook.inspect({
    kind: "table",
    sheetId: spec.name,
    range: keyRange,
    include: "values,formulas",
    tableMaxRows: 24,
    tableMaxCols: Math.min(columnCount, 16),
    maxChars: 12000,
  });
  qa.inspections[spec.name] = inspection.ndjson || "";
  const formulaInspection = await workbook.inspect({
    kind: "formula",
    sheetId: spec.name,
    range: `A1:${lastColumn}${lastRow}`,
    maxChars: 8000,
    options: { maxResults: 300 },
  });
  const formulaText = formulaInspection.ndjson || "";
  const formulaMatches = formulaText.match(
    /#(?:REF!|DIV\/0!|VALUE!|NAME\?|N\/A|NUM!|NULL!)/g,
  ) || [];
  if (formulaMatches.length > 0) {
    qa.formula_errors.push({ sheet: spec.name, matches: formulaMatches });
  }
  const previewLastColumn = spec.preview_last_column || lastColumn;
  const preview = await workbook.render({
    sheetName: spec.name,
    range: `A1:${previewLastColumn}${Math.min(lastRow, 38)}`,
    scale: 1,
    format: "png",
  });
  const previewName = `${String(sheetIndex + 1).padStart(2, "0")}-${safeFileName(spec.name)}.png`;
  await fs.writeFile(
    path.join(qaDirectory, previewName),
    new Uint8Array(await preview.arrayBuffer()),
  );
  qa.sheets.push({
    name: spec.name,
    rows: detailRows.length,
    columns: columnCount,
    preview: previewName,
  });
  if (spec.detail_preview_first_column && spec.detail_preview_last_column) {
    const detailPreview = await workbook.render({
      sheetName: spec.name,
      range: `${spec.detail_preview_first_column}1:${spec.detail_preview_last_column}${Math.min(lastRow, 38)}`,
      scale: 1,
      format: "png",
    });
    const detailName = `${String(sheetIndex + 1).padStart(2, "0")}-${safeFileName(spec.name)}-timing.png`;
    await fs.writeFile(
      path.join(qaDirectory, detailName),
      new Uint8Array(await detailPreview.arrayBuffer()),
    );
    qa.sheets[qa.sheets.length - 1].detail_preview = detailName;
  }
}

if (qa.formula_errors.length > 0) {
  throw new Error("formula errors detected in final workbook");
}
const sheetInspection = await workbook.inspect({
  kind: "sheet",
  include: "id,name",
  maxChars: 4000,
});
qa.sheet_inspection_ndjson = sheetInspection.ndjson || "";

const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);
qa.output_xlsx = outputPath;
await fs.writeFile(
  path.join(qaDirectory, "workbook_qa.json"),
  `${JSON.stringify(qa, null, 2)}\n`,
  "utf8",
);
