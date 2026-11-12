import { test as base, expect, type Locator, type Page } from '@playwright/test';
import { PNG_BASE64 } from './agent-events';

// Shared fixture for all specs. Every test automatically fails if the page:
//  * throws an uncaught exception (typical symptom of a broken dependency at runtime),
//  * logs a console error not explicitly allowed by the test (MapLibre logs its errors, too),
//  * sends a request outside localhost that no test route handled (keeps CI hermetic).
// Basemap tile servers are faked with a small PNG, and the number of tiles loaded is
// exposed so tests can check that the MapLibre web worker actually works.

type Options = { allowedConsoleErrors: RegExp[] };
type Fixtures = { pageHealth: void; mapTiles: { count: number } };

const BASEMAP_TILE_HOSTS = ['services.arcgisonline.com', 'mt1.google.com', 'a.basemaps.cartocdn.com'];
const TILE_PNG = Buffer.from(PNG_BASE64, 'base64');

export const test = base.extend<Options & Fixtures>({
  allowedConsoleErrors: [[], { option: true }],

  mapTiles: [async ({ page }, use) => {
    const tiles = { count: 0 };
    await page.route((url) => BASEMAP_TILE_HOSTS.includes(url.hostname), async (route) => {
      tiles.count += 1;
      await route.fulfill({ status: 200, headers: { 'content-type': 'image/png', 'access-control-allow-origin': '*' }, body: TILE_PNG });
    });
    await use(tiles);
  }, { auto: true }],

  pageHealth: [async ({ page, context, allowedConsoleErrors }, use, testInfo) => {
    const problems: string[] = [];

    page.on('pageerror', (error) => problems.push(`Uncaught exception: ${error.stack || error.message}`));
    page.on('console', (msg) => {
      if (msg.type() !== 'error') return;
      const text = msg.text();
      if (!allowedConsoleErrors.some((re) => re.test(text))) problems.push(`console.error: ${text}`);
    });

    // Page-level routes (fake AWS, basemap tiles) take precedence over this context-level catch-all.
    await context.route((url) => !['127.0.0.1', 'localhost'].includes(url.hostname) && url.protocol !== 'blob:', async (route) => {
      problems.push(`Unexpected external request: ${route.request().method()} ${route.request().url()}`);
      await route.abort('blockedbyclient');
    });

    await use();

    if (problems.length) {
      await testInfo.attach('page-problems', { body: problems.join('\n\n'), contentType: 'text/plain' });
    }
    expect(problems, 'The page reported runtime problems').toEqual([]);
  }, { auto: true }],
});

export { expect };

/** The chat transcript is rendered twice (visible pane + hidden print report); only the visible copy matters. */
export const visible = (locator: Locator) => locator.filter({ visible: true });

export const questionBox = (page: Page) => page.getByPlaceholder('Ask about satellite imagery...');
export const polygonBox = (page: Page) => page.getByPlaceholder('[[lon1, lat1], [lon2, lat2], ...]');
export const mapCanvas = (page: Page) => page.locator('canvas.maplibregl-canvas');

/** Wait for the map: WebGL canvas created and basemap tiles fetched by the MapLibre worker. */
export async function expectMapReady(page: Page, tiles: { count: number }) {
  await expect(mapCanvas(page)).toBeVisible();
  await expect.poll(() => tiles.count, { message: 'basemap tiles requested by MapLibre', timeout: 20_000 }).toBeGreaterThan(0);
}

export async function setPolygon(page: Page, coordinates: number[][]) {
  await polygonBox(page).fill(JSON.stringify(coordinates));
  await expect(page.getByText(/✓ Polygon selected \([\d,.]+ km²\)/)).toBeVisible();
}

export async function ask(page: Page, question: string) {
  await questionBox(page).fill(question);
  await page.getByRole('button', { name: 'Send' }).click();
}

/** Assert that every agent event type is rendered (shared by mocked and full-stack specs). */
export async function expectAnalysisRendered(page: Page, expected: {
  intermediateText: string; codeSnippet: string; output: string | RegExp; answer: string | RegExp;
  overlayName: string; fileName: string;
}) {
  // Metrics are the last event: once visible, the whole stream has been consumed.
  await expect(visible(page.getByText(/On demand cost: \$\d+\.\d{2}/))).toBeVisible({ timeout: 60_000 });
  await expect(visible(page.getByText(/Number of Cycles: \d+/))).toBeVisible();
  await expect(visible(page.getByText('Processing...'))).toHaveCount(0);

  await expect(visible(page.getByText(expected.intermediateText))).toBeVisible();

  // Python code, syntax-highlighted by react-syntax-highlighter.
  await expect(visible(page.locator('code.language-python')).filter({ hasText: expected.codeSnippet })).toHaveCount(1);
  // REPL output in a fenced block.
  await expect(visible(page.locator('pre code')).filter({ hasText: expected.output })).toHaveCount(1);

  // Map overlay + static image, both decoded by the browser.
  const images = visible(page.getByRole('img', { name: /^Analysis \d+$/ }));
  await expect(images).toHaveCount(2);
  for (const image of await images.all()) {
    await expect.poll(() => image.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0)).toBe(true);
  }

  // The overlay is added to the map and listed in the layer panel.
  await expect(page.getByText('Analysis Overlays')).toBeVisible();
  await expect(page.getByText(expected.overlayName, { exact: true })).toBeVisible();

  // Interactive Plotly chart actually drawn (SVG rendered, no error message).
  const plot = visible(page.locator('.js-plotly-plot')).first();
  await expect(plot.locator('svg.main-svg').first()).toBeVisible({ timeout: 30_000 });
  await expect(plot.locator('.barlayer .trace')).toHaveCount(1);
  await expect(page.getByText(/Chart (rendering )?error/)).toHaveCount(0);

  // Shared file as a download link to the pre-signed S3 URL.
  const fileLink = visible(page.getByRole('link', { name: expected.fileName }));
  await expect(fileLink).toBeVisible();
  await expect(fileLink).toHaveAttribute('href', /^https:\/\/client-file-sharing-[\w-]+\.s3\.amazonaws\.com\/.+(Signature|X-Amz-Signature)=/);

  // Final answer rendered as markdown.
  await expect(visible(page.getByText(expected.answer)).first()).toBeVisible();
}
