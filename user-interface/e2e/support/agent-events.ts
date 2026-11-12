// Agent SSE events, shaped exactly like the stream emitted by the agent service
// (`agent/geospatial_agent/agent.py`), as asserted by the agent tests in
// `agent/tests/test_agent_service.py`. The full-stack project checks the same UI
// behaviour against the real service, so drift between this file and the agent is caught.

// Area of interest (Hyde Park, London): same polygon as AOI_COORDINATES in agent/tests/offline_env.py.
export const AOI: number[][] = [
  [-0.175, 51.503],
  [-0.155, 51.503],
  [-0.155, 51.511],
  [-0.175, 51.511],
  [-0.175, 51.503],
];
// Folium bounds of the AOI: [[south, west], [north, east]].
export const AOI_FOLIUM_BOUNDS = [[51.503, -0.175], [51.511, -0.155]];

export const QUESTION = 'What is the NDVI in this area?';
export const INTERMEDIATE_TEXT = "I'll fetch the least cloudy Sentinel-2 scene and compute the NDVI.";
export const OVERLAY_NAME = 'ndvi_overlay';
export const CSV_NAME = 'ndvi_statistics.csv';

// 2x2 semi-transparent green PNG (the browser must decode it, and MapLibre must load it as an image source).
export const PNG_BASE64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAFElEQVR42mNU6lY6wcDAwMDEAAUAGBYBm5RUebcAAAAASUVORK5CYII=';

export const PYTHON_CODE =
  "result = get_satellite_data('sentinel', ['red', 'nir'], AOI_COORDINATES, start_date=date(2024, 6, 1), end_date=date(2024, 6, 30))\n" +
  "ndvi = compute_NDVI(result['data'])\nstats = ndvi.get_statistics()\nprint(f\"NDVI_MEAN={stats['mean']:.3f}\")";
export const PYTHON_OUTPUT = 'STDOUT:\nSCENE=S2B_30UXC_20240615_0_L2A NDVI_MEAN=0.500';
export const ANSWER = 'The mean NDVI in the area is **0.50**, indicating moderate to dense vegetation.';
export const PRESIGNED_URL =
  `https://client-file-sharing-test.s3.amazonaws.com/session/${CSV_NAME}?AWSAccessKeyId=AKIAEXAMPLE&Signature=abc%3D&Expires=1900000000`;

const PLOTLY_FIGURE = {
  data: [{ type: 'bar', x: ['Dense Vegetation'], y: [100] }],
  layout: { title: { text: 'NDVI classes (%)' } },
};

export const analysisEvents = (): object[] => [
  { msg_type: 'text', text: INTERMEDIATE_TEXT },
  { msg_type: 'toolUse', toolUseId: 't1', name: 'python_repl', input: { code: PYTHON_CODE } },
  { msg_type: 'toolResult', toolUseId: 't1', name: 'python_repl', status: 'success', content: [{ text: PYTHON_OUTPUT }] },
  {
    msg_type: 'toolUse', toolUseId: 't2', name: 'visualize_map_raster_layer',
    input: { image_path: `/tmp/work/${OVERLAY_NAME}.png`, folium_bounds: AOI_FOLIUM_BOUNDS }, image: PNG_BASE64,
  },
  { msg_type: 'toolUse', toolUseId: 't3', name: 'visualize_image', input: { image_path: '/tmp/work/ndvi_classes.png' }, image: PNG_BASE64 },
  {
    msg_type: 'toolUse', toolUseId: 't4', name: 'visualize_interactive_chart',
    input: { chart_path: '/tmp/work/ndvi_chart.json', chart_type: 'plotly' }, chart_spec: JSON.stringify(PLOTLY_FIGURE),
  },
  {
    msg_type: 'toolUse', toolUseId: 't5', name: 'share_file_with_client',
    input: { file_path: `/tmp/work/${CSV_NAME}` }, pre_signed_s3_url: PRESIGNED_URL,
  },
  { msg_type: 'toolResult', toolUseId: 't5', name: 'share_file_with_client', status: 'success', content: [{ text: 'null' }] },
  { msg_type: 'text', text: ANSWER },
  {
    msg_type: 'result',
    result: {
      response: ANSWER,
      metrics: { agent: { total_cycles: 3, total_duration: 4.2, on_demand_cost: 0.0123 } },
    },
  },
];

// Shape of the error event emitted by bedrock-agentcore when the agent generator raises.
export const errorEvents = (): object[] => [
  { error: 'Model unavailable', error_type: 'RuntimeError', message: 'Model unavailable' },
];

export const toSse = (events: object[]): string => events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('');
