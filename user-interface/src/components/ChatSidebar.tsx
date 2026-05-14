import { useState, useEffect, useRef } from 'react';
import {
  Container,
  Header,
  SpaceBetween,
  Button,
  Textarea,
  Box,
  Spinner,
} from '@cloudscape-design/components';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Message, ImageOverlay } from '../types';
import { streamAgentInvoke } from '../services/api';
import { useReactToPrint } from 'react-to-print';
import { useAuth } from '../auth';

import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism';


const CodeBlock = ({
  inline,
  className,
  children,
  ...props
}: {
  inline?: boolean;
  className?: string;
  children?: React.ReactNode;
}) => {
  const match = /language-(\w+)/.exec(className || '');
  
  return !inline && match ? (
    <SyntaxHighlighter
      style={vscDarkPlus}
      language={match[1]}
      PreTag="div"
      {...props}
    >
      {String(children).replace(/\n$/, '')}
    </SyntaxHighlighter>
  ) : (
    <code className={className} {...props}>
      {children}
    </code>
  );
};

interface ChatSidebarProps {
  sessionId: string;
  onSessionReset: () => void;
  coordinates?: number[][] | null;
  onCoordinatesChange?: (coords: number[][] | null) => void;
  onOverlayAdd?: (overlay: ImageOverlay) => void;
}

export function ChatSidebar({ 
  sessionId, 
  onSessionReset, 
  coordinates,
  onCoordinatesChange,
  onOverlayAdd
}: ChatSidebarProps) {
  const { signOut } = useAuth();
  const [messages, setMessages] = useState<Message[]>([]);
  const [history, setHistory] = useState<Array<[string, string]>>([]);
  const [userInput, setUserInput] = useState('');
  const [isProcessing, setIsProcessing] = useState(false);
  const [polygonText, setPolygonText] = useState('');
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const printRef = useRef<HTMLDivElement>(null);

  const handlePrint = useReactToPrint({
    contentRef: printRef,
    documentTitle: `chat-report-${sessionId.slice(0, 8)}`,
  });

  useEffect(() => {
    setMessages([]);
    setHistory([]);
    setUserInput('');
    setIsProcessing(false);
    setPolygonText('');
  }, [sessionId]);

  useEffect(() => {
    if (coordinates && coordinates.length > 0) {
      setPolygonText(JSON.stringify(coordinates));
    }
  }, [coordinates]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const sendMessage = async () => {
    if (!userInput.trim() || isProcessing || !hasPolygon) return;

    const prompt = userInput;
    setUserInput('');
    setIsProcessing(true);
    setMessages((prev) => [...prev, { role: 'user', content: prompt }]);
    
    const newHistory: Array<[string, string]> = [...history];

    try {
      for await (const event of streamAgentInvoke(prompt, sessionId, coordinates || undefined, history)) {
        if (event.type === 'image' && event.content && event.bounds && event.toolName === 'visualize_map_raster_layer') {
          console.log('🗺️ Map overlay received:', { toolName: event.toolName, bounds: event.bounds, imageName: event.imageName, imageSize: event.content.length });
          onOverlayAdd?.({ image: event.content, bounds: event.bounds as [[number, number], [number, number]], imageName: event.imageName });
        }

        if (event.type === 'image' && event.content) {
          const imageContent = event.content;
          setMessages((prev) => [...prev, {
            role: 'assistant' as const,
            content: '',
            images: [imageContent]
          }]);

        } else if (event.type === 'text' && event.content) {
          const textContent = event.content;
          setMessages((prev) => [...prev, {
            role: 'assistant' as const,
            content: textContent
          }]);
          newHistory.push(['assistant', textContent]);

        } else if (event.type === 'python_code' && event.content) {
          const codeContent = event.content;
          setMessages((prev) => [...prev, {
            role: 'assistant' as const,
            content: '```python\n' + codeContent.trim() + '\n```'
          }]);
          newHistory.push(['assistant', `python_repl Tool:\n${codeContent}`]);

        } else if (event.type === 'file_link' && event.content) {
          const url = event.content;
          const fileName = event.fileName || 'Download file';
          setMessages((prev) => [...prev, {
            role: 'assistant' as const,
            content: `📎 [${fileName}](${url})`
          }]);

        } else if (event.type === 'execution_output' && event.content) {
          const outputContent = event.content;
          setMessages((prev) => [...prev, {
            role: 'assistant' as const,
            content: '```\n' + outputContent.trim() + '\n```'
          }]);
          newHistory.push(['assistant', `Tool Output:\n${outputContent}`]);
        
        } else if (event.type === 'result' && event.content) {
          const textContent = event.content;
          setMessages((prev) => [...prev, {
            role: 'assistant' as const,
            content: textContent
          }]);

        }
      }
      
      setHistory(newHistory);

    } catch (error) {
      setMessages((prev) => [...prev, { role: 'assistant', content: `Error: ${error}` }]);
    } finally {
      setIsProcessing(false);
    }
  };

  const renderImages = (imgs: string[]) => (
    imgs.map((img, idx) => (
      <img 
        key={idx}
        src={`data:image/png;base64,${img}`}
        alt={`Analysis ${idx + 1}`}
        style={{ maxWidth: '100%', marginTop: '8px', borderRadius: '4px' }}
      />
    ))
  );

  const handlePolygonTextChange = (value: string) => {
    setPolygonText(value);
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed) && parsed.length > 0) {
        onCoordinatesChange?.(parsed);
      }
    } catch {
      // Invalid JSON, ignore
    }
  };

  const computeAreaKm2 = (coords: number[][]) => {
    const toRad = (d: number) => (d * Math.PI) / 180;
    const R = 6371;
    let area = 0;
    for (let i = 0; i < coords.length; i++) {
      const [lon1, lat1] = coords[i];
      const [lon2, lat2] = coords[(i + 1) % coords.length];
      area += toRad(lon2 - lon1) * (2 + Math.sin(toRad(lat1)) + Math.sin(toRad(lat2)));
    }
    return Math.abs((area * R * R) / 2);
  };

  const hasPolygon = coordinates && coordinates.length > 0;
  const polygonAreaKm2 = hasPolygon ? computeAreaKm2(coordinates!) : 0;

  const renderMessageContent = (msg: Message) => (
    <>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ code: CodeBlock }}>
        {msg.content}
      </ReactMarkdown>
      {msg.images && renderImages(msg.images)}
    </>
  );

  return (
    <Container header={<Header variant="h2" actions={<Button variant="normal" onClick={signOut}>Logout</Button>}>Chat</Header>}>
      <SpaceBetween size="m">
        <div style={{ maxHeight: '60vh', overflowY: 'auto' }}>
          {messages.length === 0 ? (
            <Box color="text-status-inactive">Ask about satellite imagery analysis...</Box>
          ) : (
            <SpaceBetween size="s">
              {messages.map((msg, i) => (
                <div key={i}>
                  <Box fontWeight="bold">{msg.role === 'user' ? 'You' : 'Assistant'}</Box>
                  <Box variant="div" padding={{ left: 's' }}>
                    {renderMessageContent(msg)}
                  </Box>
                </div>
              ))}
            </SpaceBetween>
          )}

          {isProcessing && (
            <Box padding={{ top: 's' }}>
              <SpaceBetween size="xs">
                <Box fontWeight="bold">Assistant <Spinner /></Box>
                <Box padding={{ left: 's' }}>Processing...</Box>
              </SpaceBetween>
            </Box>
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* Hidden printable content */}
        <div style={{ display: 'none' }}>
          <div ref={printRef} className="print-report">
            <style>{`
              @media print {
                .print-report { padding: 20px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
                .print-report h2 { margin-bottom: 4px; }
                .print-report .session-id { color: #666; font-size: 12px; margin-bottom: 16px; }
                .print-report .message { margin-bottom: 12px; page-break-inside: avoid; }
                .print-report .role { font-weight: bold; margin-bottom: 4px; }
                .print-report .content { padding-left: 12px; }
                .print-report img { max-width: 100%; page-break-inside: avoid; border-radius: 4px; margin-top: 8px; }
                .print-report pre { page-break-inside: avoid; }
              }
            `}</style>
            <h2>Chat Report</h2>
            <div className="session-id">Session: {sessionId}</div>
            {messages.map((msg, i) => (
              <div key={i} className="message">
                <div className="role">{msg.role === 'user' ? 'You' : 'Assistant'}</div>
                <div className="content">
                  {renderMessageContent(msg)}
                </div>
              </div>
            ))}
          </div>
        </div>

        <Textarea
          value={userInput}
          onChange={({ detail }) => setUserInput(detail.value)}
          placeholder="Ask about satellite imagery..."
          disabled={isProcessing}
          onKeyDown={(e) => e.detail.key === 'Enter' && !e.detail.shiftKey && sendMessage()}
        />

        <Container header={<Header variant="h3">Polygon Coordinates</Header>}>
          <SpaceBetween size="s">
            <Textarea
              value={polygonText}
              onChange={({ detail }) => handlePolygonTextChange(detail.value)}
              placeholder='[[lon1, lat1], [lon2, lat2], ...]'
              rows={3}
              disabled={isProcessing}
            />
            {hasPolygon && (
              <Box color="text-status-success" fontSize="body-s">
                ✓ Polygon selected ({polygonAreaKm2.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} km²)
              </Box>
            )}
          </SpaceBetween>
        </Container>

        {!hasPolygon && (
          <Box color="text-status-warning" fontSize="body-s">
            ⚠️ Draw a polygon on the map or enter coordinates
          </Box>
        )}
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="primary" onClick={sendMessage} disabled={isProcessing || !userInput.trim() || !hasPolygon}>
            {isProcessing ? <Spinner /> : 'Send'}
          </Button>
          <Button onClick={onSessionReset}>Reset</Button>
          <Button onClick={() => handlePrint()} disabled={messages.length === 0}>Print</Button>
        </SpaceBetween>

        <Box variant="small" color="text-status-inactive">Session: {sessionId.slice(0, 8)}...</Box>
      </SpaceBetween>
    </Container>
  );
}