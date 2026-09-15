import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const workDir = "C:/Users/johan.bano_kushkipag/Documents/Dev/kipu-alert-reviewer/outputs/kipu-roadmap-2026-08-31";
const inputPath = "C:/Users/johan.bano_kushkipag/Downloads/Agentic_Roadmap_Payments_Intelligence_H1_H2_2026.xlsx";

const input = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(input);
const roadmap = workbook.worksheets.getItem("H2 Plan Q3-Q4");

// Reutiliza el estilo visual de otro artefacto marcado como Live.
roadmap.getRange("F18").copyFrom(roadmap.getRange("F14"), "all");

roadmap.getRange("B18:K18").values = [[
  "Orchestration",
  "KIPU Guardian",
  "Monitoring",
  "Flujo revisor de alertas KIPU que consume ocurrencias de EventBridge, deduplica y aplica una política crítica determinística. Expone las alertas validadas en un dashboard con MID, fecha y hora de generación, y permite ejecutar la extracción manualmente.",
  "Live",
  "Q3",
  "MVP operativo al 31-ago-2026. Automatiza la ingestión y revisión de alertas críticas para reducir el análisis manual. Objetivo: liberar 15 h semanales; la medición de ahorro y precisión está pendiente.",
  "Integración KIPU – plataforma común de agentes",
  "Python · AWS EventBridge · SQS · Lambda · DynamoDB · Sites/D1 · GitHub",
  "MVP desplegado en ambiente dev y repositorio privado actualizado. Pendiente: integración final con la plataforma común, validación productiva y medición de falsos positivos/horas liberadas. La opción con IA está preparada en una rama separada y no está activa.",
]];

roadmap.getRange("F18").format.fill = "#C6EFCE";
roadmap.getRange("F18").format.font = { bold: true, color: "#006100" };
roadmap.getRange("E18").format.wrapText = true;
roadmap.getRange("H18:K18").format.wrapText = true;
roadmap.getRange("A18:K18").format.rowHeightPx = 112;

const outputPath = path.join(
  workDir,
  "Agentic_Roadmap_Payments_Intelligence_H1_H2_2026_actualizado_2026-08-31.xlsx",
);

const targetInspect = await workbook.inspect({
  kind: "table",
  range: "H2 Plan Q3-Q4!A17:K19",
  include: "values,formulas",
  maxChars: 16000,
  tableMaxRows: 6,
  tableMaxCols: 12,
  tableMaxCellChars: 500,
});
console.log("TARGET_AFTER");
console.log(targetInspect.ndjson);

const formulaErrors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  maxChars: 10000,
});
console.log("FORMULA_ERRORS");
console.log(formulaErrors.ndjson);

await fs.mkdir(path.join(workDir, "previews-after"), { recursive: true });
for (const sheet of workbook.worksheets.items) {
  const preview = await workbook.render({
    sheetName: sheet.name,
    autoCrop: "all",
    scale: 1,
    format: "png",
  });
  const safeName = sheet.name.replace(/[^a-z0-9_-]+/gi, "_");
  await fs.writeFile(
    path.join(workDir, "previews-after", `${safeName}.png`),
    new Uint8Array(await preview.arrayBuffer()),
  );
  console.log(`RENDERED ${sheet.name}`);
}

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(`EXPORTED ${outputPath}`);
