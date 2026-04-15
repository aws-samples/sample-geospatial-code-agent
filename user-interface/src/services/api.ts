import type { StreamEvent } from '../types';
import { fetchAuthSession } from 'aws-amplify/auth';
import { BedrockAgentCoreClient, InvokeAgentRuntimeCommand } from '@aws-sdk/client-bedrock-agentcore';

const AGENT_RUNTIME_ARN = import.meta.env.VITE_AGENT_RUNTIME_ARN || '';
const REGION = import.meta.env.VITE_AWS_REGION || 'us-east-1';
const USE_LOCAL_AGENT = !AGENT_RUNTIME_ARN;

export async function* streamAgentInvoke(
  prompt: string, 
  sessionId: string,
  coordinates?: number[][],
  history?: Array<[string, string]>
): AsyncGenerator<StreamEvent> {
  const payload: { message: string; coordinates?: number[][]; history?: Array<[string, string]> } = { message: prompt };
  if (coordinates) {
    payload.coordinates = coordinates;
  }
  if (history) {
    payload.history = history;
  }

  let reader: ReadableStreamDefaultReader<Uint8Array>;

  if (USE_LOCAL_AGENT) {
    const response = await fetch('/invocations', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
      },
      body: JSON.stringify({
        ...payload,
        sessionId,
      }),
    });

    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    if (!response.body) throw new Error('No response stream');
    reader = response.body.getReader();
  } else {
    const session = await fetchAuthSession();
    if (!session.credentials) throw new Error('No AWS credentials');

    const client = new BedrockAgentCoreClient({
      region: REGION,
      credentials: session.credentials,
    });

    const command = new InvokeAgentRuntimeCommand({
      agentRuntimeArn: AGENT_RUNTIME_ARN,
      runtimeSessionId: sessionId,
      contentType: 'application/json',
      accept: 'text/event-stream',
      payload: new TextEncoder().encode(JSON.stringify(payload)),
    });

    const response = await client.send(command);
    const stream = response.response;
    if (!stream) throw new Error('No response stream');
    reader = stream.transformToWebStream().getReader();
  }

  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';

    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;

      try {
        const event = JSON.parse(line.slice(6));
        console.log('Parsed event:', event.msg_type, event.name, event.image ? 'HAS_IMAGE' : 'NO_IMAGE');
        
        if (event.error) {
          yield { type: 'error', content: String(event.message || event.error) };
        
        } else if (event.msg_type === 'toolUse' && (event.image || event.input?.image)) {
          const imageData = event.image || event.input?.image;
          const name = event.input.image_path?.split('/').pop()?.replace(/\.[^/.]+$/, '') || 'image';
          console.log('Image event received, size:', imageData?.length);
          yield { 
            type: 'image', 
            content: imageData,
            imageName: name,
            toolName: event.name,
            bounds: event.input?.folium_bounds
          };
        
        } else if (event.msg_type === 'toolUse' && event.name === 'share_file_with_client') {
          const fileName = event.input.file_path?.split('/').pop() || 'file';
          yield { type: 'file_link', content: event.pre_signed_s3_url, fileName };

        } else if (event.msg_type === 'text' && event.text) {
          yield { type: 'text', content: event.text };
        
        } else if (event.msg_type === 'toolUse' && event.name === 'python_repl') {
          yield { type: 'python_code', content: event.input.code };
        
        } else if (event.msg_type === 'toolResult' && event.name === 'python_repl') {
          for (const result of event.content) {
              yield { type: 'execution_output', content: result.text };
          }
        
        } else if (event.msg_type === 'result') {
          const metrics = event.result.metrics.agent
          yield { type: 'result', content: `Metrics:\n * 💸 On demand cost: $${metrics.on_demand_cost.toFixed(2)}\n * 🔄 Number of Cycles: ${metrics.total_cycles}` };
        }

      } catch {}
  }
  }
}
