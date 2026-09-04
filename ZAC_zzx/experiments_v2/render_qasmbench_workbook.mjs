import fs from "node:fs/promises";
import path from "node:path";


const AGGREGATE_SCHEMA = "qasmbench-four-method-aggregate-v1";
const SHEET_NAMES = ["Summary", "Small", "Medium", "Large"];
const SCALES = { Small: "small", Medium: "medium", Large: "large" };
const EXPECTED_FINAL_ROWS = { Small: 42, Medium: 25, Large: 70 };

const METHOD_LABELS = {
  M1: "M1 ZAC",
  M2: "M2 ICCAD/QMAP A*",
  M3: "M3 Ours-NL",
  M4: "M4 Ours-LK",
};

const METHOD_STYLES = {
  M1: { fill: "#4472C4", font: "#FFFFFF" },
  M2: { fill: "#ED7D31", font: "#FFFFFF" },
  M3: { fill: "#70AD47", font: "#FFFFFF" },
  M4: { fill: "#8064A2", font: "#FFFFFF" },
};

const SUMMARY_COLUMNS = [
  { key: "benchmark_scale", label: "规模", type: "text", width: 10 },
  { key: "method", label: "方法", type: "method", width: 22 },
  { key: "official_directories", label: "官方目录数", type: "integer", width: 11 },
  { key: "selected_qasm", label: "有QASM源", type: "integer", width: 11 },
  { key: "canonical_success", label: "规范化成功", type: "integer", width: 11 },
  { key: "no_qasm_source", label: "无QASM", type: "integer", width: 10 },
  { key: "canonical_error", label: "规范化失败", type: "integer", width: 11 },
  { key: "verified_success", label: "验证成功", type: "integer", width: 11 },
  { key: "success_rate_selected_qasm", label: "覆盖率", type: "ratio", width: 11 },
  { key: "timeout", label: "Timeout", type: "integer", width: 10 },
  { key: "oom", label: "OOM", type: "integer", width: 8 },
  { key: "error", label: "Error", type: "integer", width: 8 },
  { key: "missing", label: "Missing", type: "integer", width: 9 },
  { key: "strict_paired_count", label: "严格配对数", type: "integer", width: 11 },
  { key: "fidelity_paired_count", label: "保真度配对数", type: "integer", width: 13 },
  { key: "paired_fidelity_geomean", label: "F几何均值 ↑", type: "fidelity", width: 15 },
  { key: "paired_transfers_mean", label: "Transfers均值 ↓", type: "decimal2", width: 15 },
  { key: "paired_move_batches_mean", label: "Move批次均值 ↓", type: "decimal2", width: 16 },
  { key: "paired_move_time_ms_mean", label: "Move时间均值(ms) ↓", type: "decimal3", width: 18 },
  { key: "paired_algorithm_time_s_mean", label: "算法时间均值(s) ↓", type: "decimal6", width: 18 },
  { key: "fidelity_gain_pct", label: "F相对B*收益 ↑", type: "percent100", width: 15 },
  { key: "fidelity_wins", label: "F Win", type: "integer", width: 8 },
  { key: "fidelity_ties", label: "F Tie", type: "integer", width: 8 },
  { key: "fidelity_losses", label: "F Loss", type: "integer", width: 8 },
  { key: "transfers_reduction_pct", label: "Transfers降幅 ↑", type: "percent100", width: 15 },
  { key: "transfers_wins", label: "Transfer Win", type: "integer", width: 11 },
  { key: "transfers_ties", label: "Transfer Tie", type: "integer", width: 11 },
  { key: "transfers_losses", label: "Transfer Loss", type: "integer", width: 12 },
  { key: "move_batches_reduction_pct", label: "Move批次降幅 ↑", type: "percent100", width: 15 },
  { key: "move_batches_wins", label: "批次 Win", type: "integer", width: 9 },
  { key: "move_batches_ties", label: "批次 Tie", type: "integer", width: 9 },
  { key: "move_batches_losses", label: "批次 Loss", type: "integer", width: 10 },
  { key: "move_time_ms_reduction_pct", label: "Move时间降幅 ↑", type: "percent100", width: 16 },
  { key: "move_time_ms_wins", label: "时间 Win", type: "integer", width: 9 },
  { key: "move_time_ms_ties", label: "时间 Tie", type: "integer", width: 9 },
  { key: "move_time_ms_losses", label: "时间 Loss", type: "integer", width: 10 },
  { key: "algorithm_time_s_speedup_vs_M2", label: "相对M2加速比", type: "speedup", width: 14 },
  { key: "algorithm_time_s_wins", label: "运行 Win", type: "integer", width: 9 },
  { key: "algorithm_time_s_ties", label: "运行 Tie", type: "integer", width: 9 },
  { key: "algorithm_time_s_losses", label: "运行 Loss", type: "integer", width: 10 },
];

const DETAIL_BASE_COLUMNS = [
  { key: "benchmark_directory", label: "电路", type: "text", width: 30 },
  { key: "qubits", label: "量子比特", type: "integer", width: 10 },
  { key: "gates_1q", label: "1Q门", type: "integer", width: 11 },
  { key: "gates_2q", label: "2Q门", type: "integer", width: 12 },
  { key: "canonical_status", label: "输入状态", type: "status", width: 15 },
  { key: "canonical_error", label: "输入错误", type: "text", width: 22 },
  { key: "strict_paired", label: "严格配对", type: "boolean", width: 11 },
  { key: "pairing_reason", label: "配对说明", type: "text", width: 26 },
];

const METHOD_CORE_COLUMNS = [
  { suffix: "fidelity", label: "Fidelity ↑", type: "fidelity", width: 15 },
  { suffix: "transfers", label: "Transfers ↓", type: "decimal2", width: 12 },
  { suffix: "move_batches", label: "Move批次 ↓", type: "decimal2", width: 13 },
  { suffix: "move_time_ms", label: "Move时间(ms) ↓", type: "decimal3", width: 15 },
  { suffix: "algorithm_time_s", label: "算法时间(s) ↓", type: "decimal6", width: 15 },
  { suffix: "valid_over_N", label: "valid/N", type: "text-center", width: 10 },
  { suffix: "status", label: "状态", type: "status", width: 15 },
];

const OURS_TIMING_COLUMNS = [
  { suffix: "initial_placement_s", label: "初始布局(s)", type: "decimal6", width: 14 },
  { suffix: "problem_preparation_s", label: "问题构造(s)", type: "decimal6", width: 14 },
  { suffix: "native_search_s", label: "Native搜索(s)", type: "decimal6", width: 15 },
  { suffix: "search_kernel_s", label: "Search kernel(s)", type: "decimal6", width: 16 },
  { suffix: "return_match_s", label: "RETURN匹配(s)", type: "decimal6", width: 15 },
  { suffix: "forecast_s", label: "前瞻(s)", type: "decimal6", width: 12 },
  { suffix: "result_commit_s", label: "结果提交(s)", type: "decimal6", width: 14 },
  { suffix: "routing_s", label: "最终路由(s)", type: "decimal6", width: 14 },
];


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

function safeFileName(value) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function assertObject(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
}

function requireColumns(payload, sheetName, columns) {
  const declared = payload.columns?.[sheetName];
  if (!Array.isArray(declared)) {
    throw new Error(`${sheetName} columns must be an array`);
  }
  const missing = columns.filter((column) => !declared.includes(column.key));
  if (missing.length > 0) {
    throw new Error(`${sheetName} is missing renderer columns: ${missing.join(",")}`);
  }
}

function detailColumns() {
  const columns = DETAIL_BASE_COLUMNS.map((column) => ({ ...column, group: "电路与输入" }));
  for (const method of ["M1", "M2", "M3", "M4"]) {
    for (const column of METHOD_CORE_COLUMNS) {
      columns.push({
        key: `${method}__${column.suffix}`,
        label: column.label,
        type: column.type,
        width: column.width,
        group: method,
      });
    }
    if (method === "M3" || method === "M4") {
      for (const column of OURS_TIMING_COLUMNS) {
        columns.push({
          key: `${method}__${column.suffix}`,
          label: column.label,
          type: column.type,
          width: column.width,
          group: method,
        });
      }
    }
  }
  return columns;
}

function validatePayload(payload) {
  assertObject(payload, "aggregate payload");
  if (payload.aggregate_schema !== AGGREGATE_SCHEMA) {
    throw new Error(`invalid aggregate schema: ${payload.aggregate_schema}`);
  }
  if (payload.exact_sheet_count !== 4 || JSON.stringify(payload.sheet_names) !== JSON.stringify(SHEET_NAMES)) {
    throw new Error("QASMBench aggregate must declare exactly Summary/Small/Medium/Large");
  }
  assertObject(payload.columns, "aggregate columns");
  assertObject(payload.sheets, "aggregate sheets");
  requireColumns(payload, "Summary", SUMMARY_COLUMNS);
  const detailed = detailColumns();
  for (const sheetName of SHEET_NAMES) {
    if (!Array.isArray(payload.sheets[sheetName])) {
      throw new Error(`${sheetName} rows must be an array`);
    }
  }
  for (const sheetName of ["Small", "Medium", "Large"]) {
    requireColumns(payload, sheetName, detailed);
    const expectedScale = SCALES[sheetName];
    const rows = payload.sheets[sheetName];
    if (rows.length > EXPECTED_FINAL_ROWS[sheetName]) {
      throw new Error(`${sheetName} contains more than ${EXPECTED_FINAL_ROWS[sheetName]} official directories`);
    }
    const directories = new Set();
    for (const [index, row] of rows.entries()) {
      assertObject(row, `${sheetName} row ${index + 1}`);
      if (row.benchmark_scale !== expectedScale) {
        throw new Error(`${sheetName} row ${index + 1} has scale ${row.benchmark_scale}`);
      }
      const directory = String(row.benchmark_directory || "");
      if (!directory || directories.has(directory)) {
        throw new Error(`${sheetName} has an empty or duplicate directory: ${directory}`);
      }
      directories.add(directory);
    }
  }
  const summaryKeys = new Set();
  for (const [index, row] of payload.sheets.Summary.entries()) {
    assertObject(row, `Summary row ${index + 1}`);
    const scale = String(row.benchmark_scale || "");
    const method = String(row.method || "");
    if (!Object.values(SCALES).includes(scale) || !Object.hasOwn(METHOD_LABELS, method)) {
      throw new Error(`invalid Summary identity: ${scale}/${method}`);
    }
    const key = `${scale}/${method}`;
    if (summaryKeys.has(key)) throw new Error(`duplicate Summary identity: ${key}`);
    summaryKeys.add(key);
  }
  for (const scale of Object.values(SCALES)) {
    for (const method of Object.keys(METHOD_LABELS)) {
      if (!summaryKeys.has(`${scale}/${method}`)) {
        throw new Error(`missing Summary identity: ${scale}/${method}`);
      }
    }
  }
  return {
    aggregate_schema: AGGREGATE_SCHEMA,
    sheet_names: [...SHEET_NAMES],
    exact_sheet_count: 4,
    row_counts: Object.fromEntries(SHEET_NAMES.map((name) => [name, payload.sheets[name].length])),
    complete_inventory: ["Small", "Medium", "Large"].every(
      (name) => payload.sheets[name].length === EXPECTED_FINAL_ROWS[name],
    ),
    expected_official_directory_count: 137,
    rendered_detail_column_count: detailed.length,
  };
}

function typedValue(value, type, key) {
  if (value === null || value === undefined || value === "") return null;
  if (["integer", "decimal2", "decimal3", "decimal6", "fidelity", "ratio", "speedup", "percent100"].includes(type)) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) throw new Error(`non-finite numeric value for ${key}: ${value}`);
    return type === "percent100" ? numeric / 100.0 : numeric;
  }
  if (type === "boolean") {
    if (value === true || String(value).toLowerCase() === "true") return true;
    if (value === false || String(value).toLowerCase() === "false") return false;
    throw new Error(`invalid boolean value for ${key}: ${value}`);
  }
  if (type === "method") return METHOD_LABELS[String(value)] || String(value);
  return String(value);
}

function numberFormat(type) {
  return {
    integer: "#,##0",
    decimal2: "#,##0.00",
    decimal3: "#,##0.000",
    decimal6: "#,##0.000000",
    fidelity: "0.000000E+00",
    ratio: "0.0%",
    percent100: "0.00%",
    speedup: '0.000"x"',
  }[type] || null;
}

function styleDataColumns(sheet, columns, startRow, endRow) {
  for (let index = 0; index < columns.length; index += 1) {
    const letter = columnName(index);
    const column = columns[index];
    sheet.getRange(`${letter}1:${letter}${Math.max(startRow - 1, 1)}`).format.columnWidth = column.width;
    if (endRow < startRow) continue;
    const range = sheet.getRange(`${letter}${startRow}:${letter}${endRow}`);
    const format = numberFormat(column.type);
    if (format) range.format.numberFormat = format;
    if (format) range.format.horizontalAlignment = "right";
    else if (["boolean", "text-center", "status"].includes(column.type)) range.format.horizontalAlignment = "center";
    else range.format.horizontalAlignment = "left";
    sheet.getRange(`${letter}${startRow}:${letter}${endRow}`).format.columnWidth = column.width;
  }
}

function addStatusFormatting(range) {
  const rules = [
    ["success", "#E2F0D9", "#2E5D22"],
    ["timeout", "#FFF2CC", "#7F6000"],
    ["oom", "#FCE4D6", "#9C0006"],
    ["error", "#F4CCCC", "#9C0006"],
    ["fail", "#F4CCCC", "#9C0006"],
    ["missing", "#E7E6E6", "#595959"],
    ["no_qasm_source", "#DDEBF7", "#1F4E78"],
  ];
  for (const [text, fill, color] of rules) {
    range.conditionalFormats.add("containsText", {
      text,
      format: { fill, font: { color, bold: true } },
    });
  }
}

function styleBaseBody(sheet, rangeAddress) {
  const range = sheet.getRange(rangeAddress);
  range.format = {
    font: { name: "Aptos", size: 10, color: "#1F2937" },
    verticalAlignment: "center",
    borders: {
      insideHorizontal: { style: "thin", color: "#E2E8F0" },
      bottom: { style: "thin", color: "#CBD5E1" },
    },
  };
}

async function buildSummarySheet(workbook, payload, qaDirectory) {
  const sheet = workbook.worksheets.add("Summary");
  sheet.showGridLines = false;
  const rows = payload.sheets.Summary;
  const lastColumn = columnName(SUMMARY_COLUMNS.length - 1);
  const dataStart = 4;
  const lastRow = dataStart + rows.length - 1;
  sheet.getRange(`A1:${lastColumn}1`).merge();
  sheet.getRange("A1").values = [["QASMBench Small / Medium / Large 四方法总体收益"]];
  sheet.getRange(`A2:${lastColumn}2`).merge();
  sheet.getRange("A2").values = [[
    "说明：各尺度独立汇总。Fidelity相对逐电路B*=max(M1,M2)；Transfers与Move相对逐电路Bmin=min(M1,M2)；算法时间相对M2。OOD仅退出Fidelity聚合。",
  ]];
  sheet.getRange(`A3:${lastColumn}3`).values = [[...SUMMARY_COLUMNS.map((column) => column.label)]];
  if (rows.length > 0) {
    sheet.getRange(`A${dataStart}:${lastColumn}${lastRow}`).values = rows.map(
      (row) => SUMMARY_COLUMNS.map((column) => typedValue(row[column.key], column.type, column.key)),
    );
    styleBaseBody(sheet, `A${dataStart}:${lastColumn}${lastRow}`);
    sheet.getRange(`A${dataStart}:${lastColumn}${lastRow}`).format.rowHeight = 21;
  }
  sheet.getRange(`A1:${lastColumn}1`).format = {
    fill: "#173F66",
    font: { name: "Aptos Display", size: 15, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "left",
    verticalAlignment: "center",
  };
  sheet.getRange(`A1:${lastColumn}1`).format.rowHeight = 32;
  sheet.getRange(`A2:${lastColumn}2`).format = {
    fill: "#F3F6FA",
    font: { name: "Aptos", size: 9, italic: true, color: "#526273" },
    horizontalAlignment: "left",
    verticalAlignment: "center",
    wrapText: true,
  };
  sheet.getRange(`A2:${lastColumn}2`).format.rowHeight = 31;
  sheet.getRange(`A3:${lastColumn}3`).format = {
    fill: "#285A84",
    font: { name: "Aptos", size: 10, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: { insideVertical: { style: "thin", color: "#B8C7D9" }, bottom: { style: "medium", color: "#173F66" } },
  };
  sheet.getRange(`A3:${lastColumn}3`).format.rowHeight = 43;
  styleDataColumns(sheet, SUMMARY_COLUMNS, dataStart, lastRow);
  if (rows.length > 0) {
    const methodColumn = sheet.getRange(`B${dataStart}:B${lastRow}`);
    methodColumn.format.font = { name: "Aptos", size: 10, bold: true, color: "#17324D" };
    for (let index = 0; index < rows.length; index += 1) {
      const method = String(rows[index].method || "");
      const style = METHOD_STYLES[method];
      if (style) {
        sheet.getRange(`B${dataStart + index}`).format.fill = style.fill;
        sheet.getRange(`B${dataStart + index}`).format.font = {
          name: "Aptos", size: 10, bold: true, color: style.font,
        };
      }
    }
  }
  sheet.freezePanes.freezeRows(3);
  sheet.freezePanes.freezeColumns(2);
  const verification = await verifyAndPreview(
    workbook,
    sheet,
    "Summary",
    `A1:${lastColumn}${Math.max(lastRow, 4)}`,
    qaDirectory,
    0,
  );
  return {
    name: "Summary", rows: rows.length, columns: SUMMARY_COLUMNS.length,
    ...verification,
  };
}

function contiguousGroups(columns) {
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

async function buildDetailSheet(workbook, payload, sheetName, qaDirectory, sheetIndex) {
  const columns = detailColumns();
  const rows = payload.sheets[sheetName];
  const sheet = workbook.worksheets.add(sheetName);
  sheet.showGridLines = false;
  const lastColumn = columnName(columns.length - 1);
  const dataStart = 4;
  const lastRow = dataStart + rows.length - 1;
  sheet.getRange(`A1:${lastColumn}1`).merge();
  const expected = EXPECTED_FINAL_ROWS[sheetName];
  sheet.getRange("A1").values = [[
    `QASMBench ${sheetName} 四方法逐电路结果（当前 ${rows.length}/${expected} 个官方目录）`,
  ]];
  for (const group of contiguousGroups(columns)) {
    const startLetter = columnName(group.start);
    const endLetter = columnName(group.end);
    sheet.getRange(`${startLetter}2:${endLetter}2`).merge();
    const label = METHOD_LABELS[group.group] || group.group;
    sheet.getRange(`${startLetter}2`).values = [[label]];
  }
  sheet.getRange(`A3:${lastColumn}3`).values = [[...columns.map((column) => column.label)]];
  if (rows.length > 0) {
    sheet.getRange(`A${dataStart}:${lastColumn}${lastRow}`).values = rows.map(
      (row) => columns.map((column) => typedValue(row[column.key], column.type, column.key)),
    );
    styleBaseBody(sheet, `A${dataStart}:${lastColumn}${lastRow}`);
    sheet.getRange(`A${dataStart}:${lastColumn}${lastRow}`).format.rowHeight = 21;
  }
  sheet.getRange(`A1:${lastColumn}1`).format = {
    fill: "#173F66",
    font: { name: "Aptos Display", size: 15, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "left",
    verticalAlignment: "center",
  };
  sheet.getRange(`A1:${lastColumn}1`).format.rowHeight = 32;
  sheet.getRange(`A2:${lastColumn}2`).format = {
    fill: "#5B7088",
    font: { name: "Aptos Display", size: 11, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    borders: { insideVertical: { style: "medium", color: "#FFFFFF" } },
  };
  for (const group of contiguousGroups(columns)) {
    const style = METHOD_STYLES[group.group] || { fill: "#5B7088", font: "#FFFFFF" };
    const range = sheet.getRange(`${columnName(group.start)}2:${columnName(group.end)}2`);
    range.format.fill = style.fill;
    range.format.font = { name: "Aptos Display", size: 11, bold: true, color: style.font };
  }
  sheet.getRange(`A2:${lastColumn}2`).format.rowHeight = 25;
  sheet.getRange(`A3:${lastColumn}3`).format = {
    fill: "#E8EEF6",
    font: { name: "Aptos", size: 9, bold: true, color: "#17324D" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: {
      insideVertical: { style: "thin", color: "#B8C7D9" },
      bottom: { style: "medium", color: "#7B91A8" },
    },
  };
  sheet.getRange(`A3:${lastColumn}3`).format.rowHeight = 44;
  styleDataColumns(sheet, columns, dataStart, lastRow);
  if (rows.length > 0) {
    for (let index = 0; index < columns.length; index += 1) {
      if (columns[index].type === "status") {
        addStatusFormatting(sheet.getRange(`${columnName(index)}${dataStart}:${columnName(index)}${lastRow}`));
      }
    }
  }
  sheet.freezePanes.freezeRows(3);
  sheet.freezePanes.freezeColumns(4);
  const verification = await verifyAndPreview(
    workbook,
    sheet,
    sheetName,
    `A1:${lastColumn}${Math.min(Math.max(lastRow, 4), 16)}`,
    qaDirectory,
    sheetIndex,
  );
  return {
    name: sheetName, rows: rows.length, columns: columns.length,
    ...verification,
  };
}

async function verifyAndPreview(workbook, sheet, sheetName, range, qaDirectory, index) {
  const inspection = await workbook.inspect({
    kind: "table",
    sheetId: sheetName,
    range,
    include: "values,formulas",
    tableMaxRows: 16,
    tableMaxCols: 18,
    tableMaxCellChars: 80,
    maxChars: 12000,
  });
  const errors = await workbook.inspect({
    kind: "match",
    sheetId: sheetName,
    range,
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!",
    options: { useRegex: true, maxResults: 300 },
    summary: `${sheetName} formula error scan`,
    maxChars: 6000,
  });
  const errorText = errors.ndjson || "";
  if (/#(?:REF!|DIV\/0!|VALUE!|NAME\?|N\/A|NUM!|NULL!)/.test(errorText)) {
    throw new Error(`formula error detected in ${sheetName}`);
  }
  const preview = await workbook.render({
    sheetName,
    range,
    scale: 1,
    format: "png",
  });
  const previewName = `${String(index + 1).padStart(2, "0")}-${safeFileName(sheetName)}.png`;
  await fs.writeFile(path.join(qaDirectory, previewName), new Uint8Array(await preview.arrayBuffer()));
  return {
    inspection_ndjson: inspection.ndjson || "",
    formula_error_scan_ndjson: errorText,
    preview: previewName,
  };
}

async function renderWorkbook(payload, outputPath, qaDirectory) {
  const { SpreadsheetFile, Workbook } = await import("@oai/artifact-tool");
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.mkdir(qaDirectory, { recursive: true });
  const workbook = Workbook.create();
  const qa = {
    aggregate_schema: AGGREGATE_SCHEMA,
    sheet_names: [...SHEET_NAMES],
    exact_sheet_count: 4,
    sheets: [],
    output_xlsx: outputPath,
  };
  qa.sheets.push(await buildSummarySheet(workbook, payload, qaDirectory));
  for (let index = 0; index < 3; index += 1) {
    const name = ["Small", "Medium", "Large"][index];
    qa.sheets.push(await buildDetailSheet(workbook, payload, name, qaDirectory, index + 1));
  }
  const inspection = await workbook.inspect({ kind: "sheet", include: "id,name", maxChars: 4000 });
  qa.sheet_inspection_ndjson = inspection.ndjson || "";
  const xlsx = await SpreadsheetFile.exportXlsx(workbook);
  await xlsx.save(outputPath);
  await fs.writeFile(path.join(qaDirectory, "workbook_qa.json"), `${JSON.stringify(qa, null, 2)}\n`, "utf8");
  return qa;
}

async function main() {
  const args = process.argv.slice(2);
  const validateOnly = args[0] === "--validate-only";
  const aggregateArgument = validateOnly ? args[1] : args[0];
  if (!aggregateArgument || (!validateOnly && args.length !== 3) || (validateOnly && args.length !== 2)) {
    throw new Error(
      "usage: render_qasmbench_workbook.mjs AGGREGATE_JSON OUTPUT_XLSX QA_DIRECTORY\n" +
      "   or: render_qasmbench_workbook.mjs --validate-only AGGREGATE_JSON",
    );
  }
  const aggregatePath = path.resolve(aggregateArgument);
  const payload = JSON.parse(await fs.readFile(aggregatePath, "utf8"));
  const validation = validatePayload(payload);
  if (validateOnly) {
    process.stdout.write(`${JSON.stringify(validation)}\n`);
    return;
  }
  const outputPath = path.resolve(args[1]);
  const qaDirectory = path.resolve(args[2]);
  const qa = await renderWorkbook(payload, outputPath, qaDirectory);
  process.stdout.write(`${JSON.stringify({ ...validation, qa })}\n`);
}

await main();
