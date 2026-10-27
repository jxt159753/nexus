// Trusted, offline recognition worker. No images or landmarks leave this client.
let detector;
function features(landmarks) {
  const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  return landmarks.map(points => {
    const scale = Math.max(distance(points[0], points[9]), .02);
    return {
      x: Math.max(0, Math.min(1, 1 - points[8].x)), y: Math.max(0, Math.min(1, points[8].y)),
      pinch: distance(points[4], points[8]) / scale < .35,
      open: [8, 12, 16, 20].every(tip => distance(points[tip], points[0]) > distance(points[tip - 2], points[0]) * 1.15),
      landmarks: points.map(p => ({ x: 1 - p.x, y: p.y, z: p.z })),
    };
  }).sort((a, b) => a.x - b.x);
}
self.onmessage = async ({ data }) => {
  try {
    if (data.kind === "init") {
      const { FilesetResolver, HandLandmarker } = await import("./vendor/mediapipe/vision_bundle.mjs");
      const base = new URL("./vendor/mediapipe/", self.location.href).href;
      detector = await HandLandmarker.createFromOptions(await FilesetResolver.forVisionTasks(base + "wasm"), {
        baseOptions: { modelAssetPath: base + "hand_landmarker.task", delegate: "CPU" },
        runningMode: "VIDEO", numHands: 2,
        minHandDetectionConfidence: .6, minHandPresenceConfidence: .6, minTrackingConfidence: .6,
      });
      self.postMessage({ ready: true });
    } else if (data.kind === "frame") {
      const result = detector.detectForVideo(data.bitmap, data.time_ms);
      self.postMessage({ frame: { time_ms: data.time_ms, hands: features(result.landmarks) } });
    }
  } catch (error) { self.postMessage({ error: String(error.message).slice(0, 300) }); }
  finally { data.bitmap?.close(); }
};
