import { test, ask, expect, expectAnalysisRendered, expectMapReady, setPolygon, visible } from './support/test';
import { AOI, CSV_NAME, INTERMEDIATE_TEXT, OVERLAY_NAME, QUESTION } from './support/agent-events';

// Full stack: the local-agent UI build talks (through the vite preview proxy) to the
// real agent service started by agent/tests/e2e_agent_server.py, which runs offline
// with moto-emulated AWS, a local STAC API over synthetic GeoTIFFs and a scripted LLM.
// This catches drift between the stream the agent actually emits and what the UI parses.

test('answers a question end-to-end through the real agent service', async ({ page, mapTiles }) => {
  await page.goto('/');
  await expectMapReady(page, mapTiles);
  await setPolygon(page, AOI);
  await ask(page, QUESTION);

  await expectAnalysisRendered(page, {
    intermediateText: INTERMEDIATE_TEXT,
    codeSnippet: "get_satellite_data('sentinel', ['red', 'nir'], AOI_COORDINATES",
    output: /SCENE=S2B_30UXC_20240615_0_L2A NDVI_MEAN=0\.500/,
    answer: 'The mean NDVI in the area is 0.50, indicating moderate to dense vegetation.',
    overlayName: OVERLAY_NAME,
    fileName: CSV_NAME,
  });
  await expect(visible(page.getByText(/^Error:/))).toHaveCount(0);

  // The shared file is stored under the UI session ID (sent as the AgentCore session header).
  const sessionPrefix = (await page.getByText(/^Session: [0-9a-f]{8}\.\.\.$/).innerText()).slice('Session: '.length, -3);
  await expect(visible(page.getByRole('link', { name: CSV_NAME })))
    .toHaveAttribute('href', new RegExp(`/${sessionPrefix}[0-9a-f-]{28}/${CSV_NAME}\\?`));
});
