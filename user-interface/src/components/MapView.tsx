import { useEffect, useRef, useState, useMemo } from 'react';
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import MapboxDraw from '@mapbox/mapbox-gl-draw';
import '@mapbox/mapbox-gl-draw/dist/mapbox-gl-draw.css';
import type { ImageOverlay } from '../types';
import {
  formatLayerDisplayText,
  groupLayers,
  type LayerMetadata,
} from '../utils/layerFormatting';

const MAP_CENTER: [number, number] = [-0.1276, 51.5074]
const MAP_ZOOM: number = 14

interface MapViewProps {
  onDrawnGeometry?: (geojson: any) => void;
  onDrawCleared?: () => void;
  imageOverlays?: ImageOverlay[];
  resetTrigger?: number;
  coordinates?: number[][] | null;
}

export function MapView({ onDrawnGeometry, onDrawCleared, imageOverlays, resetTrigger, coordinates }: MapViewProps) {
  const mapContainer = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const draw = useRef<MapboxDraw | null>(null);
  const [allLayers, setAllLayers] = useState<LayerMetadata[]>([]); // Persistent layer tracking
  const addedRasterUrls = useRef<Set<string>>(new Set()); // Track added raster URLs to prevent duplicates
  const addedOverlayIds = useRef<Set<string>>(new Set());
  const [rasterVisibility, setRasterVisibility] = useState<Record<string, boolean>>({});
  const [isLayerControlOpen, setIsLayerControlOpen] = useState(true);
  const [drawMode, setDrawMode] = useState<'none' | 'point' | 'polygon'>('none');
  const [hasDrawnFeatures, setHasDrawnFeatures] = useState(false);
  const [baseMapStyle, setBaseMapStyle] = useState<'dark' | 'google-roads' | 'google-satellite' | 'esri-satellite'>('esri-satellite');

  // Polygon style constants
  const POLYGON_STYLE = {
    fillColor: '#FFA500',
    fillOpacity: 0.3,
    strokeColor: '#FFA500',
    strokeWidth: 3,
    vertexColor: '#FFF',
    vertexRadius: 5
  };

  // Memoize layer groups to avoid repeated filtering on each render
  const layerGroups = useMemo(() => groupLayers(allLayers), [allLayers]);

  // Function to update base map layer
  const updateBaseMapLayer = (style: 'dark' | 'google-roads' | 'google-satellite' | 'esri-satellite') => {
    if (!map.current) return;

    const mapInstance = map.current;

    // Remove existing base map layers
    ['dark-base', 'google-roads-base', 'google-satellite-base', 'esri-satellite-base'].forEach(layerId => {
      if (mapInstance.getLayer(layerId)) {
        mapInstance.removeLayer(layerId);
      }
    });

    // Remove existing base map sources
    ['dark-source', 'google-roads-source', 'google-satellite-source', 'esri-satellite-source'].forEach(sourceId => {
      if (mapInstance.getSource(sourceId)) {
        mapInstance.removeSource(sourceId);
      }
    });

    // Get first non-basemap layer for proper ordering
    const layers = mapInstance.getStyle().layers || [];
    const baseLayerIds = ['dark-base', 'google-roads-base', 'google-satellite-base', 'esri-satellite-base'];
    const firstNonBase = layers.find(layer => !baseLayerIds.includes(layer.id));
    const beforeId = firstNonBase?.id;

    // Add new base map layer based on style
    if (style === 'dark') {
      mapInstance.addSource('dark-source', {
        type: 'raster',
        tiles: ['https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png'],
        tileSize: 256,
        attribution: '&copy; OpenStreetMap &copy; CARTO'
      });

      mapInstance.addLayer({
        id: 'dark-base',
        type: 'raster',
        source: 'dark-source',
        minzoom: 0,
        maxzoom: 22
      }, beforeId);
    } else if (style === 'google-roads') {
      mapInstance.addSource('google-roads-source', {
        type: 'raster',
        tiles: ['https://mt1.google.com/vt/lyrs=m&x={x}&y={y}&z={z}'],
        tileSize: 256,
        attribution: '&copy; Google Maps'
      });

      mapInstance.addLayer({
        id: 'google-roads-base',
        type: 'raster',
        source: 'google-roads-source',
        minzoom: 0,
        maxzoom: 22
      }, beforeId);
    } else if (style === 'google-satellite') {
      mapInstance.addSource('google-satellite-source', {
        type: 'raster',
        tiles: ['https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}'],
        tileSize: 256,
        attribution: '&copy; Google Maps'
      });

      mapInstance.addLayer({
        id: 'google-satellite-base',
        type: 'raster',
        source: 'google-satellite-source',
        minzoom: 0,
        maxzoom: 22
      }, beforeId);
    } else if (style === 'esri-satellite') {
      mapInstance.addSource('esri-satellite-source', {
        type: 'raster',
        tiles: ['https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
        tileSize: 256,
        attribution: '© Esri'
      });

      mapInstance.addLayer({
        id: 'esri-satellite-base',
        type: 'raster',
        source: 'esri-satellite-source',
        minzoom: 0,
        maxzoom: 22
      }, beforeId);
    }
  };

  useEffect(() => {
    if (!map.current || !imageOverlays?.length) return;
    const mapInstance = map.current;

    const addOverlays = () => {
      imageOverlays.forEach((overlay, i) => {
        const id = `overlay-${overlay.imageName}`;
        
        // Skip if already added to the map
        if (mapInstance.getLayer(id)) {
          console.log("Skipping overlay (already on map):", id);
          return;
        }
        
        console.log("Adding overlay:", id);
        
        const [[south, west], [north, east]] = overlay.bounds;
        const boundsArray: [number, number, number, number] = [west, south, east, north];

        // Convert base64 to blob URL (MapLibre doesn't handle data URLs well)
        const byteCharacters = atob(overlay.image);
        const byteNumbers = new Array(byteCharacters.length);
        for (let j = 0; j < byteCharacters.length; j++) {
          byteNumbers[j] = byteCharacters.charCodeAt(j);
        }
        const byteArray = new Uint8Array(byteNumbers);
        const blob = new Blob([byteArray], { type: 'image/png' });
        const blobUrl = URL.createObjectURL(blob);

        mapInstance.addSource(id, {
          type: 'image',
          url: blobUrl,
          coordinates: [[west, north], [east, north], [east, south], [west, south]]
        });
        mapInstance.addLayer({ id, type: 'raster', source: id });

        addedOverlayIds.current.add(id);
        setAllLayers(prev => [...prev, {
          id,
          sourceId: id,
          name: `${overlay.imageName}`,
          url: blobUrl,
          type: 'overlay',
          bounds: boundsArray
        }]);
      });
    };

    if (mapInstance.loaded()) {
      addOverlays();
    } else {
      mapInstance.once('load', addOverlays);
    }
  }, [imageOverlays]);



  // Handle base map style changes
  useEffect(() => {
    if (!map.current || !map.current.loaded()) return;
    updateBaseMapLayer(baseMapStyle);
  }, [baseMapStyle]);

  // Initialize map
  useEffect(() => {
    if (!mapContainer.current) return;

    map.current = new maplibregl.Map({
      container: mapContainer.current,
      style: {
        version: 8,
        sources: {},
        layers: []
      },
      center: MAP_CENTER,
      zoom: MAP_ZOOM,
      transformRequest: (url) => {
        return { url };
      },
    });

    // Add global error listener
    map.current.on('error', (e) => {
      console.error('MapLibre error:', e);
    });

    // Load initial basemap (dynamic, controlled by baseMapStyle state)
    map.current.once('load', () => {
      updateBaseMapLayer(baseMapStyle);
    });

    // Initialize draw control
    draw.current = new MapboxDraw({
      displayControlsDefault: false,
      controls: {},
      styles: [
        {
          id: 'gl-draw-polygon-fill',
          type: 'fill',
          filter: ['all', ['==', '$type', 'Polygon']],
          paint: {
            'fill-color': POLYGON_STYLE.fillColor,
            'fill-opacity': POLYGON_STYLE.fillOpacity
          }
        },
        {
          id: 'gl-draw-polygon-stroke-active',
          type: 'line',
          filter: ['all', ['==', '$type', 'Polygon']],
          paint: {
            'line-color': POLYGON_STYLE.strokeColor,
            'line-width': POLYGON_STYLE.strokeWidth
          }
        },
        {
          id: 'gl-draw-point',
          type: 'circle',
          filter: ['all', ['==', '$type', 'Point']],
          paint: {
            'circle-radius': 8,
            'circle-color': POLYGON_STYLE.fillColor
          }
        },
        {
          id: 'gl-draw-polygon-and-line-vertex-active',
          type: 'circle',
          filter: ['all', ['==', 'meta', 'vertex'], ['==', '$type', 'Point']],
          paint: {
            'circle-radius': POLYGON_STYLE.vertexRadius,
            'circle-color': POLYGON_STYLE.vertexColor
          }
        }
      ]
    });

    map.current.addControl(draw.current as any);

    // Listen for draw events
      map.current.on('draw.create', (e) => {
        setHasDrawnFeatures(true);
        // Auto-send the drawn geometry
        if (draw.current && onDrawnGeometry) {
          const data = draw.current.getAll();
          if (data.features.length > 0) {
            onDrawnGeometry(data);
            // Add drawn polygon to layer list
            setAllLayers(prev => [...prev, {
              id: 'gl-draw-polygon-fill',
              sourceId: 'mapbox-gl-draw-cold',
              name: 'Drawn Polygon',
              url: '',
              type: 'geometry'
            }]);
            setRasterVisibility(prev => ({ ...prev, 'gl-draw-polygon-fill': true }));
          }
        }
      });


    map.current.on('draw.delete', () => {
      const data = draw.current?.getAll();
      setHasDrawnFeatures(data ? data.features.length > 0 : false);
    });

    map.current.on('draw.update', () => {
      setHasDrawnFeatures(true);
    });

    return () => {
      map.current?.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Handle drawing mode changes
  const handleDrawMode = (mode: 'none' | 'point' | 'polygon') => {
    if (!draw.current) return;

    setDrawMode(mode);

    if (mode === 'none') {
      draw.current.changeMode('simple_select');
    } else if (mode === 'point') {
      draw.current.changeMode('draw_point');
    } else if (mode === 'polygon') {
      draw.current.changeMode('draw_polygon');
    }
  };

  // Clear all drawn features
  const clearDrawnFeatures = () => {
    if (!draw.current) return;
    draw.current.deleteAll();
    setHasDrawnFeatures(false);
    setDrawMode('none');
    // Remove drawn polygon from layer list
    setAllLayers(prev => prev.filter(l => l.id !== 'gl-draw-polygon-fill'));
    setRasterVisibility(prev => {
      const newVis = { ...prev };
      delete newVis['gl-draw-polygon-fill'];
      return newVis;
    });
    onDrawCleared?.();  // Notify parent to clear coordinates
  };

  // Fly to a specific layer's bounds
  const flyToLayer = (layerId: string) => {
    if (!map.current) return;
    
    const layer = allLayers.find(l => l.id === layerId);
    if (!layer || !layer.bounds) return;
    
    const mapInstance = map.current;
    const [west, south, east, north] = layer.bounds;
    
    mapInstance.fitBounds(
      [[west, south], [east, north]],
      {
        padding: 50,
        duration: 1500,
        maxZoom: 15,
      }
    );
    
    console.log(`🎯 Flying to layer:`, layer.name);
  };

  // Remove a specific layer
  const removeLayer = (layerId: string) => {
    if (!map.current) return;

    const layer = allLayers.find(l => l.id === layerId);
    if (!layer) return;

    // In removeLayer function, add this for overlay cleanup:
    if (layer.type === 'overlay' && layer.url) {
      URL.revokeObjectURL(layer.url);
    }


    const mapInstance = map.current;

    if (layer.type === 'geometry') {
      // Geometry has both fill and outline layers
      const outlineLayerId = layer.id.replace('-fill', '-outline');
      if (mapInstance.getLayer(layer.id)) {
        mapInstance.removeLayer(layer.id);
      }
      if (mapInstance.getLayer(outlineLayerId)) {
        mapInstance.removeLayer(outlineLayerId);
      }
      if (mapInstance.getSource(layer.sourceId)) {
        mapInstance.removeSource(layer.sourceId);
      }
    } else {
      // Raster layer
      if (mapInstance.getLayer(layer.id)) {
        mapInstance.removeLayer(layer.id);
      }
      if (mapInstance.getSource(layer.sourceId)) {
        mapInstance.removeSource(layer.sourceId);
      }
      if (layer.type === 'raster') addedRasterUrls.current.delete(layer.url);
      if (layer.type === 'overlay') addedOverlayIds.current.delete(layer.id);
      // Remove URL from tracking ref
      addedRasterUrls.current.delete(layer.url);
    }
    
    setAllLayers(prev => prev.filter(l => l.id !== layerId));
    setRasterVisibility(prev => {
      const newVis = { ...prev };
      delete newVis[layerId];
      return newVis;
    });
    
    console.log(`🗑️ Removed layer:`, layer.name);
  };

  // Clear all layers from map (basemap is dynamic and separate)
  const clearAllLayers = () => {
    if (!map.current) return;

    const mapInstance = map.current;
    const count = allLayers.length;

    allLayers.forEach(layer => {
      if (layer.type === 'geometry') {
        // Geometry has both fill and outline layers
        const outlineLayerId = layer.id.replace('-fill', '-outline');
        if (mapInstance.getLayer(layer.id)) {
          mapInstance.removeLayer(layer.id);
        }
        if (mapInstance.getLayer(outlineLayerId)) {
          mapInstance.removeLayer(outlineLayerId);
        }
        if (mapInstance.getSource(layer.sourceId)) {
          mapInstance.removeSource(layer.sourceId);
        }
      } else {
        // Raster layer
        if (mapInstance.getLayer(layer.id)) {
          mapInstance.removeLayer(layer.id);
        }
        if (mapInstance.getSource(layer.sourceId)) {
          mapInstance.removeSource(layer.sourceId);
        }
      }
    });

    // Clear all layers (basemap is dynamic and not tracked here)
    setAllLayers([]);
    setRasterVisibility({});
    allLayers.forEach(layer => {
      if (layer.type === 'overlay' && layer.url) {
        URL.revokeObjectURL(layer.url);
      }
    });
    addedRasterUrls.current.clear(); // Clear the URL tracking ref
    
    // Clear drawn polygon features
    clearDrawnFeatures();

    console.log(`🗑️ Cleared ${count} layers`);
  };

  // Handle layer visibility toggles (both rasters and geometries)
  useEffect(() => {
    if (!map.current) return;

    const mapInstance = map.current;

    // Update visibility for each layer
    Object.entries(rasterVisibility).forEach(([layerId, isVisible]) => {
      const visibility = isVisible ? 'visible' : 'none';
      
      // Update the main layer
      if (mapInstance.getLayer(layerId)) {
        mapInstance.setLayoutProperty(layerId, 'visibility', visibility);
      }
      
      // For geometry layers, also update the outline layer
      if (layerId.includes('geometry') && layerId.includes('-fill')) {
        const outlineLayerId = layerId.replace('-fill', '-outline');
        if (mapInstance.getLayer(outlineLayerId)) {
          mapInstance.setLayoutProperty(outlineLayerId, 'visibility', visibility);
        }
      }
      
      // For drawn polygon, update all draw control layers
      if (layerId === 'gl-draw-polygon-fill') {
        const drawLayerIds = [
          'gl-draw-polygon-fill.cold', 'gl-draw-polygon-fill.hot',
          'gl-draw-polygon-stroke-active.cold', 'gl-draw-polygon-stroke-active.hot',
          'gl-draw-point.cold', 'gl-draw-point.hot',
          'gl-draw-polygon-and-line-vertex-active.cold', 'gl-draw-polygon-and-line-vertex-active.hot'
        ];
        drawLayerIds.forEach(drawLayerId => {
          if (mapInstance.getLayer(drawLayerId)) {
            mapInstance.setLayoutProperty(drawLayerId, 'visibility', visibility);
          }
        });
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rasterVisibility]);

  // Handle reset trigger
  useEffect(() => {
    if (resetTrigger && resetTrigger > 0) {
      clearAllLayers();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resetTrigger]);

  // Center map on polygon centroid when coordinates change
  useEffect(() => {
    if (!map.current || !draw.current || !coordinates || coordinates.length === 0) return;

    const lons = coordinates.map(c => c[0]);
    const lats = coordinates.map(c => c[1]);
    const centerLon = lons.reduce((a, b) => a + b, 0) / lons.length;
    const centerLat = lats.reduce((a, b) => a + b, 0) / lats.length;

    map.current.flyTo({ center: [centerLon, centerLat], zoom: 12, duration: 1500 });

    // Add polygon to draw control (making it editable)
    const existingFeatures = draw.current.getAll();
    
    // Check if polygon already exists with same coordinates
    const firstFeature = existingFeatures.features[0];
    const isSamePolygon = firstFeature && 
      firstFeature.geometry.type === 'Polygon' &&
      JSON.stringify(firstFeature.geometry.coordinates[0]) === JSON.stringify(coordinates);
    
    if (!isSamePolygon) {
      draw.current.deleteAll();
      draw.current.add({
        type: 'Feature',
        geometry: {
          type: 'Polygon',
          coordinates: [coordinates]
        },
        properties: {}
      });
      setHasDrawnFeatures(true);
    }
  }, [coordinates]);

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%' }}>
      <div
        ref={mapContainer}
        style={{
          width: '100%',
          height: '100%',
          minHeight: '600px',
        }}
      />

      {/* Layer control panel */}
      {allLayers.length > 0 && (
        <div
          style={{
            position: 'absolute',
            top: '16px',
            right: '16px',
            backgroundColor: '#FFFFFF',
            borderRadius: '8px',
            boxShadow: '0 4px 8px rgba(0,0,0,0.16)',
            zIndex: 1,
            maxHeight: '80vh',
            overflowY: 'auto',
            fontFamily: 'Roboto, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
          }}
        >
          {/* Header with toggle and clear button */}
          <div style={{ display: 'flex', alignItems: 'center', borderBottom: '1px solid #E0E0E0' }}>
            <button
              onClick={() => setIsLayerControlOpen(!isLayerControlOpen)}
              style={{
                flex: 1,
                padding: '16px',
                backgroundColor: 'transparent',
                border: 'none',
                cursor: 'pointer',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                fontWeight: 500,
                fontSize: '16px',
                color: '#1C1B1F',
                transition: 'background-color 200ms cubic-bezier(0.4, 0.0, 0.2, 1)',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.backgroundColor = 'rgba(0, 0, 0, 0.04)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.backgroundColor = 'transparent';
              }}
            >
              <span>Layers ({allLayers.length})</span>
              <span style={{ fontSize: '12px', color: '#424242' }}>
                {isLayerControlOpen ? '▼' : '▶'}
              </span>
            </button>
            {allLayers.length > 0 && (
              <button
                onClick={clearAllLayers}
                style={{
                  padding: '12px 16px',
                  backgroundColor: '#000000',
                  color: '#FFFFFF',
                  border: 'none',
                  borderRadius: '4px',
                  cursor: 'pointer',
                  fontSize: '14px',
                  fontWeight: 500,
                  marginRight: '8px',
                  transition: 'all 200ms cubic-bezier(0.4, 0.0, 0.2, 1)',
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.backgroundColor = '#424242';
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.backgroundColor = '#000000';
                }}
                title="Clear all layers"
              >
                Clear
              </button>
            )}
          </div>

            {/* Layer controls (collapsible) */}
            {isLayerControlOpen && (
              <div
                style={{
                  padding: '0 12px 12px 12px',
                  minWidth: '250px',
                }}
              >
                {/* Satellite Images (TCI) - All rasters except basemap and spectral indices */}
                {layerGroups.tci.length > 0 && (
                  <div style={{ marginBottom: '16px' }}>
                    <div style={{ fontWeight: 500, fontSize: '14px', marginBottom: '8px', color: '#424242', marginTop: '12px' }}>
                      Satellite Images (TCI)
                    </div>
                    {layerGroups.tci.map((layer) => {
                      const isVisible = rasterVisibility[layer.id] !== false;
                      const displayText = formatLayerDisplayText(layer, 'tci');

                      return (
                        <div
                          key={layer.id}
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: '6px',
                            fontSize: '12px',
                            marginBottom: '6px',
                            paddingLeft: '6px',
                            padding: '6px 8px',
                            borderRadius: '4px',
                            transition: 'background-color 200ms cubic-bezier(0.4, 0.0, 0.2, 1)',
                          }}
                          onMouseEnter={(e) => {
                            e.currentTarget.style.backgroundColor = 'rgba(0, 0, 0, 0.04)';
                          }}
                          onMouseLeave={(e) => {
                            e.currentTarget.style.backgroundColor = 'transparent';
                          }}
                        >
                          <input
                            type="checkbox"
                            checked={isVisible}
                            onChange={(e) => {
                              e.stopPropagation();
                              setRasterVisibility(prev => ({ ...prev, [layer.id]: e.target.checked }));
                            }}
                            style={{ cursor: 'pointer', width: '18px', height: '18px' }}
                          />
                          <span 
                            style={{ flex: 1, fontWeight: 400, cursor: 'pointer', color: '#1C1B1F' }} 
                            onClick={() => flyToLayer(layer.id)}
                            title="Click to zoom to this layer"
                          >
                            {displayText}
                          </span>
                          <button
                            onClick={() => removeLayer(layer.id)}
                            style={{
                              padding: '4px 8px',
                              backgroundColor: '#000000',
                              color: '#FFFFFF',
                              border: 'none',
                              borderRadius: '4px',
                              cursor: 'pointer',
                              fontSize: '12px',
                              fontWeight: 500,
                              transition: 'background-color 200ms cubic-bezier(0.4, 0.0, 0.2, 1)',
                            }}
                            onMouseEnter={(e) => {
                              e.currentTarget.style.backgroundColor = '#424242';
                            }}
                            onMouseLeave={(e) => {
                              e.currentTarget.style.backgroundColor = '#000000';
                            }}
                            title="Remove layer"
                          >
                            ×
                          </button>
                        </div>
                      );
                    })}
                  </div>
                )}

                {/* Spectral Indices (NDVI, NBR, NDWI) */}
                {layerGroups.spectralIndices.length > 0 && (
                  <div style={{ marginBottom: '16px' }}>
                    <div style={{ fontWeight: 500, fontSize: '14px', marginBottom: '8px', color: '#424242' }}>
                      Spectral Indices
                    </div>
                    {layerGroups.spectralIndices.map((layer) => {
                      const isVisible = rasterVisibility[layer.id] !== false;
                      const displayText = formatLayerDisplayText(layer, 'spectral');

                      return (
                        <div
                          key={layer.id}
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: '6px',
                            fontSize: '12px',
                            marginBottom: '6px',
                            paddingLeft: '6px',
                            padding: '6px 8px',
                            borderRadius: '4px',
                            transition: 'background-color 200ms cubic-bezier(0.4, 0.0, 0.2, 1)',
                          }}
                          onMouseEnter={(e) => {
                            e.currentTarget.style.backgroundColor = 'rgba(0, 0, 0, 0.04)';
                          }}
                          onMouseLeave={(e) => {
                            e.currentTarget.style.backgroundColor = 'transparent';
                          }}
                        >
                          <input
                            type="checkbox"
                            checked={isVisible}
                            onChange={(e) => {
                              e.stopPropagation();
                              setRasterVisibility(prev => ({ ...prev, [layer.id]: e.target.checked }));
                            }}
                            style={{ cursor: 'pointer', width: '18px', height: '18px' }}
                          />
                          <span
                            style={{ flex: 1, fontWeight: 400, cursor: 'pointer', color: '#1C1B1F' }}
                            onClick={() => flyToLayer(layer.id)}
                            title="Click to zoom to this layer"
                          >
                            {displayText}
                          </span>
                          <button
                            onClick={() => removeLayer(layer.id)}
                            style={{
                              padding: '4px 8px',
                              backgroundColor: '#000000',
                              color: '#FFFFFF',
                              border: 'none',
                              borderRadius: '4px',
                              cursor: 'pointer',
                              fontSize: '12px',
                              fontWeight: 500,
                              transition: 'background-color 200ms cubic-bezier(0.4, 0.0, 0.2, 1)',
                            }}
                            onMouseEnter={(e) => {
                              e.currentTarget.style.backgroundColor = '#424242';
                            }}
                            onMouseLeave={(e) => {
                              e.currentTarget.style.backgroundColor = '#000000';
                            }}
                            title="Remove layer"
                          >
                            ×
                          </button>
                        </div>
                      );
                    })}
                  </div>
                )}

                {/* Geometry Layers */}
                {layerGroups.geometries.length > 0 && (
                  <div>
                    <div style={{ fontWeight: 500, fontSize: '14px', marginBottom: '8px', color: '#424242' }}>
                      Geometries
                    </div>
                    {layerGroups.geometries.map((layer) => {
                      const isVisible = rasterVisibility[layer.id] !== false;

                      return (
                        <div
                          key={layer.id}
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: '6px',
                            fontSize: '12px',
                            marginBottom: '6px',
                            padding: '8px',
                            borderRadius: '4px',
                            transition: 'background-color 200ms cubic-bezier(0.4, 0.0, 0.2, 1)',
                          }}
                          onMouseEnter={(e) => {
                            e.currentTarget.style.backgroundColor = 'rgba(0, 0, 0, 0.04)';
                          }}
                          onMouseLeave={(e) => {
                            e.currentTarget.style.backgroundColor = 'transparent';
                          }}
                        >
                          <input
                            type="checkbox"
                            checked={isVisible}
                            onChange={(e) => {
                              e.stopPropagation();
                              setRasterVisibility(prev => ({ ...prev, [layer.id]: e.target.checked }));
                            }}
                            style={{ cursor: 'pointer', width: '18px', height: '18px' }}
                          />
                          <span 
                            style={{ flex: 1, fontWeight: 400, cursor: 'pointer', color: '#1C1B1F' }} 
                            onClick={() => flyToLayer(layer.id)}
                            title="Click to zoom to this layer"
                          >
                            {layer.name}
                          </span>
                          <button
                            onClick={() => removeLayer(layer.id)}
                            style={{
                              padding: '4px 8px',
                              backgroundColor: '#000000',
                              color: '#FFFFFF',
                              border: 'none',
                              borderRadius: '4px',
                              cursor: 'pointer',
                              fontSize: '12px',
                              fontWeight: 500,
                              transition: 'background-color 200ms cubic-bezier(0.4, 0.0, 0.2, 1)',
                            }}
                            onMouseEnter={(e) => {
                              e.currentTarget.style.backgroundColor = '#424242';
                            }}
                            onMouseLeave={(e) => {
                              e.currentTarget.style.backgroundColor = '#000000';
                            }}
                            title="Remove layer"
                          >
                            ×
                          </button>
                        </div>
                      );
                    })}
                  </div>
                )}

                {/* Overlays */}
                {layerGroups.overlays.length > 0 && (
                  <div>
                    <div style={{ fontWeight: 500, fontSize: '14px', marginBottom: '8px', color: '#424242' }}>
                      Analysis Overlays
                    </div>
                    {layerGroups.overlays.map((layer) => {
                      const isVisible = rasterVisibility[layer.id] !== false;
                      return (
                        <div key={layer.id} style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px', marginBottom: '6px', padding: '6px 8px', borderRadius: '4px' }}>
                          <input type="checkbox" checked={isVisible} onChange={(e) => setRasterVisibility(prev => ({ ...prev, [layer.id]: e.target.checked }))} style={{ cursor: 'pointer', width: '18px', height: '18px' }} />
                          <span style={{ flex: 1, cursor: 'pointer' }} onClick={() => flyToLayer(layer.id)}>{layer.name}</span>
                          <button onClick={() => removeLayer(layer.id)} style={{ padding: '4px 8px', backgroundColor: '#000', color: '#fff', border: 'none', borderRadius: '4px', cursor: 'pointer' }}>×</button>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
        </div>
      )}

      {/* Drawing controls panel */}
      <div
        style={{
          position: 'absolute',
          top: '16px',
          left: '16px',
          backgroundColor: '#FFFFFF',
          borderRadius: '8px',
          boxShadow: '0 4px 8px rgba(0,0,0,0.16)',
          zIndex: 1,
          fontFamily: 'Roboto, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
          padding: '10px',
          minWidth: '160px',
        }}
      >
        <div style={{ fontWeight: 500, fontSize: '14px', marginBottom: '8px', color: '#1C1B1F', textAlign: 'center' }}>
          Draw
        </div>

        {/* Drawing mode buttons */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', marginBottom: hasDrawnFeatures ? '8px' : '0' }}>
          
          <button
            onClick={() => handleDrawMode('polygon')}
            style={{
              padding: '8px 12px',
              backgroundColor: drawMode === 'polygon' ? '#FFA500' : '#F5F5F5',
              color: drawMode === 'polygon' ? '#FFFFFF' : '#1C1B1F',
              border: 'none',
              borderRadius: '4px',
              cursor: 'pointer',
              fontSize: '13px',
              fontWeight: 500,
              transition: 'all 200ms cubic-bezier(0.4, 0.0, 0.2, 1)',
            }}
            onMouseEnter={(e) => {
              if (drawMode !== 'polygon') {
                e.currentTarget.style.backgroundColor = '#E0E0E0';
              }
            }}
            onMouseLeave={(e) => {
              if (drawMode !== 'polygon') {
                e.currentTarget.style.backgroundColor = '#F5F5F5';
              }
            }}
          >
            ⬟ Polygon
          </button>

          {drawMode !== 'none' && (
            <button
              onClick={() => handleDrawMode('none')}
              style={{
                padding: '6px 12px',
                backgroundColor: '#F5F5F5',
                color: '#1C1B1F',
                border: 'none',
                borderRadius: '4px',
                cursor: 'pointer',
                fontSize: '12px',
                fontWeight: 400,
                transition: 'all 200ms cubic-bezier(0.4, 0.0, 0.2, 1)',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.backgroundColor = '#E0E0E0';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.backgroundColor = '#F5F5F5';
              }}
            >
              Cancel
            </button>
          )}
        </div>

        {/* Action buttons */}
        {hasDrawnFeatures && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', borderTop: '1px solid #E0E0E0', paddingTop: '8px' }}>
            

            <button
              onClick={clearDrawnFeatures}
              style={{
                padding: '6px 12px',
                backgroundColor: '#F5F5F5',
                color: '#D32F2F',
                border: 'none',
                borderRadius: '4px',
                cursor: 'pointer',
                fontSize: '12px',
                fontWeight: 400,
                transition: 'all 200ms cubic-bezier(0.4, 0.0, 0.2, 1)',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.backgroundColor = '#FFEBEE';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.backgroundColor = '#F5F5F5';
              }}
            >
              🗑️ Clear
            </button>
          </div>
        )}
      </div>

      {/* Basemap Switcher - Bottom Right */}
      <div
        style={{
          position: 'absolute',
          bottom: '40px',
          right: '16px',
          backgroundColor: '#FFFFFF',
          borderRadius: '6px',
          boxShadow: '0 2px 6px rgba(0,0,0,0.15)',
          zIndex: 1,
          fontFamily: 'Roboto, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
        }}
      >
        <select
          value={baseMapStyle}
          onChange={(e) => setBaseMapStyle(e.target.value as 'dark' | 'google-roads' | 'google-satellite' | 'esri-satellite')}
          style={{
            padding: '6px 10px',
            fontSize: '12px',
            fontWeight: 500,
            color: '#1C1B1F',
            backgroundColor: '#FFFFFF',
            border: 'none',
            borderRadius: '6px',
            cursor: 'pointer',
            outline: 'none',
            fontFamily: 'inherit',
          }}
        >
          <option value="dark">Dark</option>
          <option value="google-roads">Roads</option>
          <option value="google-satellite">Google Satellite</option>
          <option value="esri-satellite">Esri Satellite</option>
        </select>
      </div>
    </div>
  );
}
