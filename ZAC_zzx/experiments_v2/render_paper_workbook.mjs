import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";


const [aggregateArg, outputArg, qaArg, nodeModulesArg] = process.argv.slice(2);
if (!aggregateArg || !outputArg || !qaArg || !nodeModulesArg) {
  throw new Error(
    "usage: render_paper_workbook.mjs AGGREGATE_DIR OUTPUT_XLSX QA_DIRECTORY NODE_MODULES",
  );
}
const aggregateDirectory = path.resolve(aggregateArg);
const outputPath = path.resolve(outputArg);
const qaDirectory = path.resolve(qaArg);
const requireFromRuntime = createRequire(
  path.join(path.resolve(nodeModulesArg), "artifact-entry.cjs"),
);
const artifactEntry = requireFromRuntime.resolve("@oai/artifact-tool");
const { SpreadsheetFile, Workbook } = await import(artifactEntry);

const SHEETS = [
  { name: "ZAC18", dataset: "zac18", file: "zac18.csv", rows: 18 },
  { name: "QMAP154", dataset: "qmap154", file: "qmap154.csv", rows: 154 },
];
const METHODS = ["M1", "M2", "M3", "M4"];
const METHOD_LABELS = {
  M1: "M1 ZAC",
  M2: "M2 ICCAD/QMAP A*",
  M3: "M3 GA-NL",
  M4: "M4 GA-LK",
};
const GROUP_COLORS = {
  base: "#5B7088", M1: "#4472C4", M2: "#ED7D31",
  M3: "#70AD47", M4: "#8064A2",
};

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

function numberOrNull(value, label) {
  if (value === null || value === undefined || value === "") return null;
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) throw new Error(`non-finite ${label}: ${value}`);
  return numeric;
}

function textOrEmpty(value) {
  if (value === null || value === undefined) return "";
  return String(value);
}

function coreColumns(method) {
  return [
    { key: `${method}__fidelity`, label: "Fidelity", kind: "fidelity", width: 15 },
    { key: `${method}__transfers`, label: "Transfers", kind: "count", width: 13 },
    { key: `${method}__idle_exposures`, label: "Idle exposure", kind: "count", width: 14 },
    { key: `${method}__move_batches`, label: "MOVE批次", kind: "count", width: 12 },
    { key: `${method}__move_time_us`, label: "MOVE时间(ms)", kind: "move_ms", width: 15 },
    { key: `${method}__algorithm_time_s`, label: "算法时间(s)", kind: "time", width: 15 },
    { key: `${method}__status`, label: "状态", kind: "status", width: 15 },
    { key: `${method}__valid_over_N`, label: "valid/N", kind: "text-center", width: 10 },
  ];
}

function timingColumns(method) {
  return [
    { key: `${method}__initial_placement_s`, label: "初始布局(s)", kind: "time", width: 14 },
    { key: `${method}__transition_decision_s`, label: "逐层决策(s,独立)", kind: "time", width: 17 },
    { key: `${method}__problem_preparation_s`, label: "问题构造(s)", kind: "time", width: 14 },
    { key: `${method}__search_kernel_s`, label: "搜索核(s)", kind: "time", width: 14 },
    { key: `${method}__return_match_s`, label: "RETURN(s,嵌套)", kind: "time", width: 16 },
    { key: `${method}__forecast_s`, label: "前瞻(s,嵌套)", kind: "time", width: 16 },
    { key: `${method}__result_commit_s`, label: "结果提交(s)", kind: "time", width: 14 },
    { key: `${method}__routing_s`, label: "最终路由(s)", kind: "time", width: 14 },
  ];
}

const COLUMNS = [
  { key: "circuit", label: "电路", kind: "text", width: 30 },
  { key: "qubits", label: "量子比特", kind: "integer", width: 10 },
  { key: "gates_1q", label: "1Q门", kind: "integer", width: 11 },
  { key: "gates_2q", label: "2Q门", kind: "integer", width: 11 },
  ...coreColumns("M1"),
  ...coreColumns("M2"),
  ...coreColumns("M3"),
  ...timingColumns("M3"),
  ...coreColumns("M4"),
  ...timingColumns("M4"),
];
if (COLUMNS.length !== 52 || columnName(COLUMNS.length - 1) !== "AZ") {
  throw new Error("paper workbook must contain exactly 52 visible columns");
}

async function csvRecords(csvPath) {
  const text = await fs.readFile(csvPath, "utf8");
  const imported = await Workbook.fromCSV(text, { sheetName: "Imported" });
  const used = imported.worksheets.getItem("Imported").getUsedRange(true);
  if (!used) throw new Error(`empty CSV: ${csvPath}`);
  const values = used.values;
  const headers = values[0].map((value) => String(value));
  const headerSet = new Set(headers);
  for (const column of COLUMNS) {
    if (!headerSet.has(column.key)) {
      throw new Error(`CSV lacks required column ${column.key}: ${csvPath}`);
    }
  }
  for (const method of METHODS) {
    for (const suffix of ["valid", "N"]) {
      const key = `${method}__${suffix}`;
      if (!headerSet.has(key)) throw new Error(`CSV lacks required column ${key}`);
    }
  }
  return values.slice(1).map((row) => {
    const record = {};
    for (let index = 0; index < headers.length; index += 1) {
      record[headers[index]] = row[index];
    }
    return record;
  });
}

function validateRecords(spec, records, summary) {
  if (records.length !== spec.rows) {
    throw new Error(`${spec.name} must contain ${spec.rows} circuits, found ${records.length}`);
  }
  if (summary.circuit_N !== spec.rows) {
    throw new Error(`${spec.name} summary circuit_N differs from CSV`);
  }
  const seen = new Set();
  for (const record of records) {
    if (record.dataset !== spec.dataset) throw new Error(`${spec.name} dataset drift`);
    const circuit = textOrEmpty(record.circuit);
    if (!circuit || seen.has(circuit)) throw new Error(`${spec.name} duplicate/empty circuit: ${circuit}`);
    seen.add(circuit);
    for (const method of METHODS) {
      const expectedN = ["M1", "M2"].includes(method) ? 1 : 3;
      const n = numberOrNull(record[`${method}__N`], `${method} N`);
      const valid = numberOrNull(record[`${method}__valid`], `${method} valid`);
      if (n !== expectedN || valid === null || valid < 0 || valid > n) {
        throw new Error(`${spec.name}/${circuit} invalid ${method} valid/N`);
      }
      if (textOrEmpty(record[`${method}__valid_over_N`]) !== `${valid}/${n}`) {
        throw new Error(`${spec.name}/${circuit} ${method} valid_over_N drift`);
      }
      const fidelity = numberOrNull(record[`${method}__fidelity`], `${method} Fidelity`);
      if (fidelity !== null && (fidelity < 0 || fidelity > 1)) {
        throw new Error(`${spec.name}/${circuit} ${method} Fidelity outside [0,1]`);
      }
    }
  }
  for (const method of METHODS) {
    if (summary.methods[method].coverage.N !== spec.rows) {
      throw new Error(`${spec.name} ${method} coverage N drift`);
    }
  }
}

function displayValue(record, column) {
  const raw = record[column.key];
  if (["status", "text", "text-center"].includes(column.kind)) return textOrEmpty(raw);
  const numeric = numberOrNull(raw, column.key);
  if (numeric === null) return null;
  return column.kind === "move_ms" ? numeric / 1000.0 : numeric;
}

function numberFormat(kind) {
  return {
    fidelity: "0.000000E+00",
    count: "#,##0.00",
    integer: "#,##0",
    move_ms: "#,##0.000",
    time: "#,##0.000000",
  }[kind] || null;
}

function addStatusFormatting(range) {
  const rules = [
    ["success", "#E2F0D9", "#2E5D22"],
    ["partial", "#FFF2CC", "#7F6000"],
    ["timeout", "#FFF2CC", "#7F6000"],
    ["oom", "#F4CCCC", "#9C0006"],
    ["error", "#F4CCCC", "#9C0006"],
    ["fail", "#F4CCCC", "#9C0006"],
    ["duplicate", "#F4CCCC", "#9C0006"],
    ["missing", "#E7E6E6", "#595959"],
  ];
  for (const [text, fill, color] of rules) {
    range.conditionalFormats.add("containsText", {
      text, format: { fill, font: { color, bold: true } },
    });
  }
}

function summaryRows(datasetSummary) {
  const fields = [
    ["Fidelity几何均值", "fidelity_geometric_mean", (value) => value],
    ["Transfers算术均值", "transfers_arithmetic_mean", (value) => value],
    ["Idle exposure算术均值", "idle_exposures_arithmetic_mean", (value) => value],
    ["MOVE批次算术均值", "move_batches_arithmetic_mean", (value) => value],
    ["MOVE时间均值(ms)", "move_time_us_arithmetic_mean", (value) => value === null ? null : value / 1000],
    ["算法时间均值(s)", "algorithm_time_s_arithmetic_mean", (value) => value],
  ];
  const rows = fields.map(([label, key, transform]) => [
    label,
    ...METHODS.map((method) => {
      const value = datasetSummary.methods[method][key];
      return value === null || value === undefined ? null : transform(Number(value));
    }),
  ]);
  rows.push(["成功覆盖/N", ...METHODS.map((method) => {
    const coverage = datasetSummary.methods[method].coverage;
    return `${coverage.success_circuits}/${coverage.N}`;
  })]);
  rows.push(["完整种子覆盖/N", ...METHODS.map((method) => {
    const coverage = datasetSummary.methods[method].coverage;
    return `${coverage.complete_seed_circuits}/${coverage.N}`;
  })]);
  return rows;
}

async function buildSheet(workbook, spec, records, datasetSummary, index, qa) {
  const sheet = workbook.worksheets.add(spec.name);
  sheet.showGridLines = false;
  const lastRow = 14 + records.length;
  sheet.getRange("A1:AZ1").merge();
  sheet.getRange("A1").values = [[
    `${spec.name} 四方法论文实验结果（M1/M2 seed0一次；M3/M4三种子中位数）`,
  ]];
  sheet.getRange("A2:AZ2").merge();
  sheet.getRange("A2").values = [[
    "Fidelity仅在M1/M2各一次成功、M3/M4三种子完整且线性模型有效的严格共同集合上聚合；覆盖率独立报告。RETURN匹配与前瞻均嵌套在搜索核时间中，不得重复相加。",
  ]];
  sheet.getRange("A3:E3").values = [["指标", ...METHODS.map((method) => METHOD_LABELS[method])]];
  sheet.getRange("A4:E11").values = summaryRows(datasetSummary);

  sheet.getRange("G3:M3").values = [[
    "方法", "Fidelity比", "收益", "CI下界", "CI上界", "胜/平/负", "严格N",
  ]];
  const comparisonRows = ["M3", "M4"].map((method) => {
    const comparison = datasetSummary.comparisons[`${method}_vs_Bstar`];
    const bootstrap = comparison.bootstrap;
    return [METHOD_LABELS[method], comparison.geometric_mean_ratio, null,
      bootstrap.ci95_low, bootstrap.ci95_high,
      `${comparison.wins}/${comparison.ties}/${comparison.losses}`,
      comparison.strict_common_linear_N];
  });
  sheet.getRange("G4:M5").values = comparisonRows;
  sheet.getRange("I4:I5").formulas = [['=IF(H4="","",H4-1)'], ['=IF(H5="","",H5-1)']];
  sheet.getRange("A12:AZ12").merge();
  sheet.getRange("A12").values = [[
    `严格共同集合N=${datasetSummary.strict_common_linear_N}；各方法success与完整种子覆盖见上表；逐电路失败不删除、不补跑。`,
  ]];

  const groups = [
    ["A13:D13", "电路", "base"],
    ["E13:L13", "M1 ZAC", "M1"],
    ["M13:T13", "M2 ICCAD/QMAP A*", "M2"],
    ["U13:AB13", "M3 GA-NL 核心指标", "M3"],
    ["AC13:AJ13", "M3 阶段时间", "M3"],
    ["AK13:AR13", "M4 GA-LK 核心指标", "M4"],
    ["AS13:AZ13", "M4 阶段时间", "M4"],
  ];
  for (const [address, label, colorKey] of groups) {
    const range = sheet.getRange(address);
    range.merge();
    range.values = [[label]];
    range.format = {
      fill: GROUP_COLORS[colorKey],
      font: { name: "Aptos Display", size: 11, bold: true, color: "#FFFFFF" },
      horizontalAlignment: "center", verticalAlignment: "center",
    };
  }
  sheet.getRange("A14:AZ14").values = [COLUMNS.map((column) => column.label)];
  const matrix = records.map((record) => COLUMNS.map((column) => displayValue(record, column)));
  sheet.getRange(`A15:AZ${lastRow}`).values = matrix;

  sheet.getRange("A1:AZ1").format = {
    fill: "#173F66",
    font: { name: "Aptos Display", size: 15, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "left", verticalAlignment: "center", rowHeight: 30,
  };
  sheet.getRange("A2:AZ2").format = {
    fill: "#F1F5F9", font: { name: "Aptos", size: 9, italic: true, color: "#526273" },
    horizontalAlignment: "left", verticalAlignment: "center", wrapText: true, rowHeight: 28,
  };
  sheet.getRange("A3:E3").format = {
    fill: "#285A84", font: { name: "Aptos Display", size: 10, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center", verticalAlignment: "center",
  };
  for (let offset = 0; offset < 4; offset += 1) {
    sheet.getRange(`${columnName(offset + 1)}3`).format.fill = GROUP_COLORS[`M${offset + 1}`];
  }
  sheet.getRange("A4:E11").format = {
    fill: "#EDF3FA", font: { name: "Aptos", size: 10, color: "#1F2937" },
    borders: { insideHorizontal: { style: "thin", color: "#C8D3E0" } },
  };
  sheet.getRange("A4:A11").format.font = { name: "Aptos", size: 10, bold: true, color: "#17324D" };
  sheet.getRange("B4:E4").format.numberFormat = "0.000000E+00";
  sheet.getRange("B5:E8").format.numberFormat = "#,##0.00";
  sheet.getRange("B9:E9").format.numberFormat = "#,##0.000";
  sheet.getRange("B4:E11").format.horizontalAlignment = "right";
  sheet.getRange("G3:M3").format = {
    fill: "#5B7088", font: { name: "Aptos Display", size: 10, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center", verticalAlignment: "center",
  };
  sheet.getRange("G4:M5").format = {
    fill: "#F7F9FC", font: { name: "Aptos", size: 10, color: "#1F2937" },
    borders: { insideHorizontal: { style: "thin", color: "#D8E0EA" } },
  };
  sheet.getRange("H4:H5").format.numberFormat = '0.0000"x"';
  sheet.getRange("I4:I5").format.numberFormat = "0.00%";
  sheet.getRange("J4:K5").format.numberFormat = "0.0000";
  sheet.getRange("I4:I5").conditionalFormats.add("cellIs", {
    operator: "greaterThanOrEqual", formula: 0,
    format: { fill: "#E2F0D9", font: { color: "#2E5D22", bold: true } },
  });
  sheet.getRange("I4:I5").conditionalFormats.add("cellIs", {
    operator: "lessThan", formula: 0,
    format: { fill: "#F4CCCC", font: { color: "#9C0006", bold: true } },
  });
  sheet.getRange("A12:AZ12").format = {
    fill: "#FFF2CC", font: { name: "Aptos", size: 9, color: "#7F6000", italic: true },
    horizontalAlignment: "left", verticalAlignment: "center", wrapText: true, rowHeight: 24,
  };
  sheet.getRange("A14:AZ14").format = {
    fill: "#E8EEF6", font: { name: "Aptos", size: 9, bold: true, color: "#17324D" },
    horizontalAlignment: "center", verticalAlignment: "center", wrapText: true,
    borders: { bottom: { style: "medium", color: "#7B91A8" } }, rowHeight: 38,
  };
  sheet.getRange(`A15:AZ${lastRow}`).format = {
    font: { name: "Aptos", size: 9, color: "#1F2937" },
    verticalAlignment: "center", rowHeight: 19,
    borders: { insideHorizontal: { style: "thin", color: "#E2E8F0" } },
  };
  for (let columnIndex = 0; columnIndex < COLUMNS.length; columnIndex += 1) {
    const column = COLUMNS[columnIndex];
    const letter = columnName(columnIndex);
    sheet.getRange(`${letter}13:${letter}${lastRow}`).format.columnWidth = column.width;
    const body = sheet.getRange(`${letter}15:${letter}${lastRow}`);
    const format = numberFormat(column.kind);
    if (format) {
      body.format.numberFormat = format;
      body.format.horizontalAlignment = "right";
    } else if (["status", "text-center"].includes(column.kind)) {
      body.format.horizontalAlignment = "center";
    } else {
      body.format.horizontalAlignment = "left";
    }
  }
  for (const letter of ["K", "S", "AA", "AQ"]) {
    addStatusFormatting(sheet.getRange(`${letter}15:${letter}${lastRow}`));
  }
  sheet.freezePanes.freezeRows(14);
  sheet.freezePanes.freezeColumns(4);

  const inspection = await workbook.inspect({
    kind: "table", sheetId: spec.name, range: `A1:AZ${Math.min(lastRow, 32)}`,
    include: "values,formulas", tableMaxRows: 32, tableMaxCols: 52,
    maxChars: 30000,
  });
  const errorInspection = await workbook.inspect({
    kind: "match", sheetId: spec.name, range: `A1:AZ${lastRow}`,
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!",
    options: { useRegex: true, maxResults: 300 }, maxChars: 6000,
  });
  const errorText = errorInspection.ndjson || "";
  const matches = errorText.match(/#(?:REF!|DIV\/0!|VALUE!|NAME\?|N\/A|NUM!|NULL!)/g) || [];
  if (matches.length) qa.formula_errors.push({ sheet: spec.name, matches });

  const previews = [];
  for (const [suffix, range] of [
    ["core", `A1:AB${Math.min(lastRow, 32)}`],
    ["timing", `AC1:AZ${Math.min(lastRow, 32)}`],
  ]) {
    const preview = await workbook.render({ sheetName: spec.name, range, scale: 1, format: "png" });
    const name = `${String(index + 1).padStart(2, "0")}-${spec.name.toLowerCase()}-${suffix}.png`;
    await fs.writeFile(path.join(qaDirectory, name), new Uint8Array(await preview.arrayBuffer()));
    previews.push(name);
    qa.previews.push(name);
  }
  qa.sheets.push({
    name: spec.name, rows: records.length, columns: COLUMNS.length,
    first_circuit: records[0].circuit, last_circuit: records.at(-1).circuit,
    previews, inspection_ndjson: inspection.ndjson || "",
    formula_error_scan_ndjson: errorText,
  });
}

await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.mkdir(qaDirectory, { recursive: true });
const summary = JSON.parse(
  await fs.readFile(path.join(aggregateDirectory, "main_summary.json"), "utf8"),
);
if (
  summary.protocol !== "paper-zh-v1-main-summary-v1" ||
  JSON.stringify(Object.keys(summary.datasets).sort()) !== JSON.stringify(["qmap154", "zac18"])
) {
  throw new Error("invalid paper main_summary.json");
}
const workbook = Workbook.create();
const qa = {
  protocol: "paper-zh-v1-two-sheet-workbook-v1",
  column_count: COLUMNS.length,
  sheets: [], previews: [], formula_errors: [],
  output_xlsx: outputPath,
};
for (let index = 0; index < SHEETS.length; index += 1) {
  const spec = SHEETS[index];
  const records = await csvRecords(path.join(aggregateDirectory, spec.file));
  validateRecords(spec, records, summary.datasets[spec.dataset]);
  await buildSheet(workbook, spec, records, summary.datasets[spec.dataset], index, qa);
}
if (qa.formula_errors.length) throw new Error("formula errors detected in paper workbook");
const sheetInspection = await workbook.inspect({ kind: "sheet", include: "id,name", maxChars: 4000 });
qa.sheet_inspection_ndjson = sheetInspection.ndjson || "";
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);
try {
  const packageJson = JSON.parse(
    await fs.readFile(path.join(path.resolve(nodeModulesArg), "@oai", "artifact-tool", "package.json"), "utf8"),
  );
  qa.artifact_tool_version = packageJson.version;
} catch {
  qa.artifact_tool_version = "unknown";
}
await fs.writeFile(
  path.join(qaDirectory, "workbook_qa.json"), `${JSON.stringify(qa, null, 2)}\n`, "utf8",
);
