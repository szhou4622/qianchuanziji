"""Exercise real tooltip/CSV boundaries with inert malicious strings."""
import csv
import io
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import Mock, patch

from api.operation_events import export_operation_events_csv
from production_v1a.reports import OperationReportService
from tests import test_dashboard_null_metrics as dashboard_tests
from utils.csv_safety import spreadsheet_cell

ROOT = Path(__file__).resolve().parents[1]
UNSAFE = ['=1+1', '+SUM(1,2)', '-1+2', '@SUM(1,2)', '\t=1', '\r=1', '\n=1', '  =1', '\ufeff=1']
SAFE = ['中文素材.mp4', '带"引号,逗号\n下一行', '&lt;标签&gt;', '', '1234567890123456789']


class DesktopExportSecurityTests(unittest.TestCase):
    def test_csv_exports_preserve_text_but_neutralize_formulas(self):
        for text in UNSAFE + SAFE:
            with self.subTest(text=text):
                expected = "'" + text if text in UNSAFE else text
                with patch('api.operation_events.query_operation_events_page', return_value=(1, [{
                    'material_name': text, 'object_name': text, 'summary': text, 'plan_name': text}])):
                    result = export_operation_events_csv()
                parsed = list(csv.reader(io.StringIO(result.lstrip('\ufeff'))))
                for index in (8, 10, 14, 20):
                    self.assertEqual(expected, parsed[1][index])
                service = OperationReportService(Mock(), Mock())
                columns = ['event_time_beijing', 'account_name', 'aavid', 'source_plan_name',
                           'source', 'action_type', 'result_status', 'control_task_id', 'error_message']
                with patch.object(service, 'query_events', return_value=[dict.fromkeys(columns, text)]):
                    exported = service.export_csv('test-owner').decode('utf-8-sig')
                self.assertEqual([expected] * len(columns), list(csv.reader(io.StringIO(exported)))[1])
        self.assertEqual(-12.5, spreadsheet_cell(-12.5))

    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_frontend_uses_same_csv_text_contract(self):
        source = (ROOT / 'production_v1a_frontend/src/csv.ts').read_text(encoding='utf-8')
        source = source.replace('export function', 'function').replace(': unknown', '')
        code = source + '\nconst values = JSON.parse(require("fs").readFileSync(0,"utf8")); process.stdout.write(JSON.stringify(values.map(csvCell)));'
        values = UNSAFE + SAFE + [-12.5]
        result = subprocess.run([shutil.which('node'), '-e', code], input=json.dumps(values),
                                text=True, encoding='utf-8', capture_output=True, timeout=20, check=True)
        cells = json.loads(result.stdout)
        for value, encoded in zip(values, cells):
            expected = "'" + value if value in UNSAFE else str(value)
            self.assertEqual(expected, next(csv.reader(io.StringIO(encoded)))[0])

    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_actual_pie_tooltip_encodes_full_masked_and_fallback_names(self):
        dashboard_tests.DashboardNullMetricTests.run_js(self,
            ['escapeHtml', 'optionalMetricNumber', 'maskMaterialTitle', 'getMaterialDisplayTitle',
             'buildPieDataFromTopList', 'renderPieChart'], r'''
            let captured, maskMaterialNames = false, pieChartData=[], dashboardScopeGeneration=1;
            const pieChartInstance={setOption:o=>captured=o,resize(){}};
            const updateLatestCrawlCostDisplay=async()=>{};
            let input=[];
            const fetchTop20ByCost=async()=>input;
            (async()=>{
              const names=['素材.mp4<img src=x onerror="globalThis.bad=1">',
                '素材(<svg onload="globalThis.bad=1">).mp4', '<img src=x>', '&lt;svg&gt;', '正常中文素材.mp4'];
              for (const masked of [true,false]) {
                maskMaterialNames=masked;
                for (const name of names) {
                  input=[{title:name,currentCost:12}];
                  await renderPieChart();
                  const data=captured.series[0].data[0];
                  const html=captured.tooltip.formatter({data,name,value:12,percent:25});
                  assert.ok(html.includes(escapeHtml(data.fullName)));
                  assert.ok(!/<(?:img|svg)\b/i.test(html));
                  const fallback=captured.tooltip.formatter({name,value:12,percent:25});
                  assert.ok(fallback.includes(escapeHtml(name)));
                  assert.ok(!/<(?:img|svg)\b/i.test(fallback));
                }
              }
            })().catch(e=>{console.error(e);process.exitCode=1});
            ''')
