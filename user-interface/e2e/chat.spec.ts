import type { Page, Request } from '@playwright/test';
import {
  test, expect, ask, expectAnalysisRendered, expectMapReady, mapCanvas, polygonBox, questionBox, setPolygon, visible,
} from './support/test';
import {
  analysisEvents, AOI, CSV_NAME, errorEvents, INTERMEDIATE_TEXT, OVERLAY_NAME, QUESTION, toSse,
} from './support/agent-events';

// Local-agent build (empty VITE_AGENT_RUNTIME_ARN): the UI POSTs to /invocations.
// The agent is mocked here to check map, rendering and conversation behaviour in detail.

async function mockAgent(page: Page, respond: (request: Request) => { status?: number; events?: object[] }) {
  const requests: any[] = [];
  await page.route('**/invocations', async (route) => {
    requests.push({ body: route.request().postDataJSON(), headers: route.request().headers() });
    const { status = 200, events = [] } = respond(route.request());
    await route.fulfill({
      status,
      headers: { 'content-type': status === 200 ? 'text/event-stream' : 'application/json' },
      body: status === 200 ? toSse(events) : '{"message":"Internal Server Error"}',
    });
  });
  return requests;
}

test.describe('chat and map (local agent, mocked stream)', () => {
  test('renders the map and the empty chat without sign-in', async ({ page, mapTiles }) => {
    await page.goto('/');
    await expect(page).toHaveTitle('Geospatial Agent');
    await expect(page.getByRole('heading', { name: 'Chat' })).toBeVisible();
    await expect(page.getByText('Ask about satellite imagery analysis...')).toBeVisible();
    await expect(page.getByText('Draw a polygon on the map or enter coordinates')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Sign In' })).toHaveCount(0);

    await expectMapReady(page, mapTiles);
    await expect(page.getByRole('button', { name: '⬟ Polygon' })).toBeVisible();

    // A question alone is not enough: an area of interest is required.
    await questionBox(page).fill(QUESTION);
    await expect(page.getByRole('button', { name: 'Send' })).toBeDisabled();
    await setPolygon(page, AOI);
    await expect(page.getByRole('button', { name: 'Send' })).toBeEnabled();
    await expect(page.getByRole('button', { name: 'Print' })).toBeDisabled();
  });

  test('draws a polygon on the map', async ({ page, mapTiles }) => {
    await page.goto('/');
    await expectMapReady(page, mapTiles);

    await page.getByRole('button', { name: '⬟ Polygon' }).click();
    const box = (await mapCanvas(page).boundingBox())!;
    const cx = box.x + box.width / 2;
    const cy = box.y + box.height / 2;
    const vertices: Array<[number, number]> = [[cx - 80, cy - 60], [cx + 80, cy - 60], [cx + 80, cy + 60], [cx - 80, cy + 60]];
    for (const [x, y] of vertices.slice(0, -1)) {
      await page.mouse.click(x, y);
      await page.waitForTimeout(150); // distinct clicks, not a double-click
    }
    // Double-clicking the last vertex finishes the polygon and fires `draw.create`.
    const [lastX, lastY] = vertices[vertices.length - 1];
    await page.mouse.dblclick(lastX, lastY);

    await expect(page.getByText(/✓ Polygon selected \([\d,.]+ km²\)/)).toBeVisible();
    await expect(page.getByText('Drawn Polygon', { exact: true })).toBeVisible();
    const ring = JSON.parse(await polygonBox(page).inputValue()) as number[][];
    expect(ring).toHaveLength(5);
    expect(ring[0]).toEqual(ring[4]);
    for (const [lon, lat] of ring) {
      // Around the initial map center (London).
      expect(Math.abs(lon - -0.1276)).toBeLessThan(0.05);
      expect(Math.abs(lat - 51.5074)).toBeLessThan(0.05);
    }

    // The draw panel's Clear button (the layer panel's one has a "Clear all layers" title).
    await page.locator('button:not([title])', { hasText: /Clear$/ }).click();
    await expect(page.getByText('Draw a polygon on the map or enter coordinates')).toBeVisible();
  });

  test('renders every agent event type', async ({ page, mapTiles }) => {
    const requests = await mockAgent(page, () => ({ events: analysisEvents() }));
    await page.goto('/');
    await expectMapReady(page, mapTiles);
    await setPolygon(page, AOI);
    await ask(page, QUESTION);

    await expect(visible(page.getByText(QUESTION))).toBeVisible();
    await expectAnalysisRendered(page, {
      intermediateText: INTERMEDIATE_TEXT,
      codeSnippet: 'get_satellite_data',
      output: 'NDVI_MEAN=0.500',
      answer: /mean NDVI in the area is 0\.50/,
      overlayName: OVERLAY_NAME,
      fileName: CSV_NAME,
    });
    await expect(visible(page.locator('strong', { hasText: '0.50' }))).toBeVisible();
    await expect(visible(page.getByText('On demand cost: $0.01'))).toBeVisible();
    await expect(visible(page.getByText('Number of Cycles: 3'))).toBeVisible();
    await expect(page.getByRole('button', { name: 'Print' })).toBeEnabled();

    expect(requests).toHaveLength(1);
    expect(requests[0].body).toMatchObject({ message: QUESTION, coordinates: AOI, history: [] });
    expect(requests[0].body.sessionId).toMatch(/^[0-9a-f-]{36}$/);
    // The session is also sent as the AgentCore session header, as InvokeAgentRuntime does.
    expect(requests[0].headers['x-amzn-bedrock-agentcore-runtime-session-id']).toBe(requests[0].body.sessionId);
  });

  test('keeps conversation history within a session and resets it', async ({ page }) => {
    const requests = await mockAgent(page, () => ({ events: [{ msg_type: 'text', text: 'Reply from the agent' }] }));
    await page.goto('/');
    await setPolygon(page, AOI);

    await ask(page, 'First question');
    await expect(visible(page.getByText('Reply from the agent'))).toHaveCount(1);
    await expect(questionBox(page)).toBeEnabled();

    // Enter submits (Shift+Enter would insert a newline).
    await questionBox(page).fill('Second question');
    await questionBox(page).press('Enter');
    await expect(visible(page.getByText('Reply from the agent'))).toHaveCount(2);

    expect(requests).toHaveLength(2);
    expect(requests[1].body.sessionId).toBe(requests[0].body.sessionId);
    expect(requests[1].body.history).toEqual([['assistant', 'Reply from the agent']]);

    await page.getByRole('button', { name: 'Reset' }).click();
    await expect(page.getByText('Ask about satellite imagery analysis...')).toBeVisible();
    await expect(page.getByText('Draw a polygon on the map or enter coordinates')).toBeVisible();

    await setPolygon(page, AOI);
    await ask(page, 'Third question');
    await expect(visible(page.getByText('Reply from the agent'))).toHaveCount(1);
    expect(requests[2].body.sessionId).not.toBe(requests[0].body.sessionId);
    expect(requests[2].body.history).toEqual([]);
  });

  test('shows errors streamed by the agent', async ({ page }) => {
    await mockAgent(page, () => ({ events: errorEvents() }));
    await page.goto('/');
    await setPolygon(page, AOI);
    await ask(page, 'Trigger an agent error');
    await expect(visible(page.getByText('Error: Model unavailable'))).toBeVisible();
    await expect(questionBox(page)).toBeEnabled();
  });

  test.describe('when the agent endpoint fails', () => {
    test.use({ allowedConsoleErrors: [/Failed to load resource: the server responded with a status of 500/] });

    test('shows the HTTP error and recovers', async ({ page }) => {
      await mockAgent(page, () => ({ status: 500 }));
      await page.goto('/');
      await setPolygon(page, AOI);
      await ask(page, 'Trigger an HTTP error');
      await expect(visible(page.getByText('Error: Error: HTTP 500'))).toBeVisible();
      await expect(questionBox(page)).toBeEnabled();
    });
  });
});
