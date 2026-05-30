const RTVI_VERSION = "1.4.0";
const SPEAKING_LEVEL = 0.08;
const MEDIAPIPE_MODULE_URL = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@latest/+esm";
const MEDIAPIPE_WASM_ROOT = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@latest/wasm";
const MEDIAPIPE_MODEL_URL =
  "https://storage.googleapis.com/mediapipe-tasks/object_detector/efficientdet_lite0_uint8.tflite";
const MEDIAPIPE_GESTURE_MODEL_URL =
  "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task";
const VISUAL_DEMO_CATEGORIES = ["cup", "bottle"];
const VISUAL_DETECTION_THRESHOLD = 0.25;
const VISUAL_DETECTION_INTERVAL_MS = 1200;
const VISUAL_DETECTION_WINDOW_MS = 4500;
const VISUAL_DETECTION_REQUIRED_HITS = 1;
const GESTURE_LANGUAGE_MAP = {
  Pointing_Up: { language: "English", code: "en" },
  "Pointing Up": { language: "English", code: "en" },
  PointingUp: { language: "English", code: "en" },
  Victory: { language: "Spanish", code: "es" },
  ILoveYou: { language: "Spanish", code: "es" },
};
const GESTURE_DETECTION_THRESHOLD = 0.55;
const GESTURE_DETECTION_INTERVAL_MS = 500;
const GESTURE_DETECTION_WINDOW_MS = 2200;
const GESTURE_DETECTION_REQUIRED_HITS = 2;

const state = {
  pc: null,
  dc: null,
  localStream: null,
  sessionId: null,
  pcId: null,
  pingTimer: null,
  iceFlushTimer: null,
  pendingCandidates: [],
  canSendIceCandidates: false,
  muted: false,
  connected: false,
  dataChannelOpen: false,
  agentSpeaking: false,
  userSpeakingFromRTVI: false,
  userSpeakingFromMeter: false,
  cameraEnabled: true,
  lastMessageByRole: new Map(),
  audioContext: null,
  analyser: null,
  meterData: null,
  meterAnimation: null,
  agentVideoCheckTimer: null,
  agentAudioRetryTimer: null,
  micHealthTimer: null,
  avatarConfigTimer: null,
  visionFilesetPromise: null,
  visualDetector: null,
  visualDetectorPromise: null,
  visualDetectionTimer: null,
  visualDetectionHits: [],
  visualObservationPending: false,
  visualObservationSent: false,
  visualCueMessageShown: false,
  visualLastRunAt: 0,
  visualLastDetections: [],
  visualCanvas: null,
  visualCanvasContext: null,
  visualDetectorLoading: false,
  gestureRecognizer: null,
  gestureRecognizerPromise: null,
  gestureDetectionTimer: null,
  gestureDetectionHits: [],
  gestureLanguagePending: false,
  gestureLanguageSent: false,
  gestureCueMessageShown: false,
  gestureLastRunAt: 0,
  gestureLastResult: null,
  gestureRecognizerLoading: false,
  activeLanguage: "English",
};

const elements = {
  prejoinShell: document.getElementById("prejoinShell"),
  callShell: document.getElementById("callShell"),
  previewVideo: document.getElementById("previewVideo"),
  previewEmpty: document.getElementById("previewEmpty"),
  previewStatus: document.getElementById("previewStatus"),
  avatarStatus: document.getElementById("avatarStatus"),
  previewMeter: document.getElementById("previewMeter"),
  joinButton: document.getElementById("joinButton"),
  previewMuteButton: document.getElementById("previewMuteButton"),
  previewCameraButton: document.getElementById("previewCameraButton"),
  previewVisualBadge: document.getElementById("previewVisualBadge"),
  previewGestureBadge: document.getElementById("previewGestureBadge"),
  previewDetectionBox: document.getElementById("previewDetectionBox"),
  callDetectionBox: document.getElementById("callDetectionBox"),
  callVisualBadge: document.getElementById("callVisualBadge"),
  callGestureBadge: document.getElementById("callGestureBadge"),
  agentAudioStatus: document.getElementById("agentAudioStatus"),
  audioRetryButton: document.getElementById("audioRetryButton"),
  speakerPermissionButton: document.getElementById("speakerPermissionButton"),
  callMicMeter: document.getElementById("callMicMeter"),
  agentVideo: document.getElementById("agentVideo"),
  agentAudio: document.getElementById("agentAudio"),
  selfView: document.getElementById("selfView"),
  stageEmpty: document.getElementById("stageEmpty"),
  callAvatarStatus: document.getElementById("callAvatarStatus"),
  messages: document.getElementById("messages"),
  statusDot: document.getElementById("statusDot"),
  statusText: document.getElementById("statusText"),
  muteButton: document.getElementById("muteButton"),
  hangupButton: document.getElementById("hangupButton"),
  micSelect: document.getElementById("micSelect"),
  cameraSelect: document.getElementById("cameraSelect"),
  speakerSelect: document.getElementById("speakerSelect"),
  callMicSelect: document.getElementById("callMicSelect"),
  callCameraSelect: document.getElementById("callCameraSelect"),
  callSpeakerSelect: document.getElementById("callSpeakerSelect"),
  chatForm: document.getElementById("chatForm"),
  chatInput: document.getElementById("chatInput"),
  sendButton: document.getElementById("sendButton"),
  agentSpeaker: document.getElementById("agentSpeaker"),
  userSpeaker: document.getElementById("userSpeaker"),
  agentSpeakerText: document.getElementById("agentSpeakerText"),
  userSpeakerText: document.getElementById("userSpeakerText"),
  userWave: document.getElementById("userWave"),
};

createMeterBars(elements.previewMeter);
createMeterBars(elements.callMicMeter);

elements.joinButton.addEventListener("click", startCall);
elements.previewMuteButton.addEventListener("click", togglePreviewMic);
elements.previewCameraButton.addEventListener("click", togglePreviewCamera);
elements.audioRetryButton.addEventListener("click", () => void ensureAgentAudioPlaying("manual retry"));
elements.speakerPermissionButton.addEventListener("click", () => void requestSpeakerOutput());
elements.muteButton.addEventListener("click", toggleMute);
elements.hangupButton.addEventListener("click", endCall);
elements.chatForm.addEventListener("submit", sendChatMessage);
elements.micSelect.addEventListener("change", () => void handleDeviceSelect("audio", elements.micSelect.value));
elements.cameraSelect.addEventListener("change", () => void handleDeviceSelect("video", elements.cameraSelect.value));
elements.speakerSelect.addEventListener("change", () => void handleDeviceSelect("speaker", elements.speakerSelect.value));
elements.callMicSelect.addEventListener("change", () => void handleDeviceSelect("audio", elements.callMicSelect.value));
elements.callCameraSelect.addEventListener("change", () => void handleDeviceSelect("video", elements.callCameraSelect.value));
elements.callSpeakerSelect.addEventListener("change", () => void handleDeviceSelect("speaker", elements.callSpeakerSelect.value));
window.addEventListener("beforeunload", () => void cleanup());

if (navigator.mediaDevices?.enumerateDevices) {
  navigator.mediaDevices.addEventListener("devicechange", () => void refreshDevices());
  void loadDemoConfig();
  void initPreview();
}

async function loadDemoConfig() {
  try {
    const response = await fetch("/demo/config", { cache: "no-store" });
    if (!response.ok) throw new Error(`Config failed: ${response.status}`);
    const config = await response.json();
    setAvatarStatus(config.avatar);
    return config;
  } catch {
    setAvatarStatus({ provider: "unknown", enabled: false, configured: false });
    return null;
  }
}

async function initPreview() {
  try {
    setPreviewStatus("Preparing devices", true);
    elements.joinButton.disabled = true;
    void loadVisualDetector().catch(() => {
      setVisualBadge("error", "Visual detection unavailable");
    });
    void loadGestureRecognizer().catch(() => {
      setGestureBadge("error", "Gesture detection unavailable");
    });
    await refreshDevices();
    if (!state.localStream) {
      state.localStream = await getLocalMedia();
    }
    assignLocalStream(state.localStream);
    await refreshDevices();
    await applySpeakerSelection();
    await startMicMeter(state.localStream);
    setPreviewMediaButtons();
    void startVisualDetection();
    void startGestureDetection();
    setPreviewStatus("Ready to join", true);
    elements.joinButton.disabled = false;
  } catch (error) {
    setPreviewStatus(error.message || "Camera or microphone unavailable", false);
    elements.joinButton.disabled = false;
  }
}

async function startCall() {
  try {
    state.visualObservationSent = false;
    state.gestureLanguageSent = false;
    state.gestureCueMessageShown = false;
    unlockAgentAudio();
    ensureMicEnabledForJoin();
    resetMessages("Connecting to the pharmacy agent...");
    showCall();
    void loadDemoConfig();
    startAvatarStatusPolling();
    setStatus("Requesting media");
    setButtons({ connecting: true });
    elements.joinButton.disabled = true;
    elements.sendButton.disabled = true;

    if (!state.localStream) {
      state.localStream = await getLocalMedia();
      assignLocalStream(state.localStream);
      await startMicMeter(state.localStream);
    }
    await resumeMicMeter();
    startMicHealthMonitor();

    const startResponse = await fetch("/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        transport: "webrtc",
        enableDefaultIceServers: true,
        body: { client: "bayview-video-demo" },
      }),
    });

    if (!startResponse.ok) {
      throw new Error(`Start failed: ${startResponse.status}`);
    }

    const startData = await startResponse.json();
    state.sessionId = startData.sessionId;

    state.pc = new RTCPeerConnection(startData.iceConfig || undefined);
    wirePeerConnection();

    const audioTransceiver = state.pc.addTransceiver("audio", { direction: "sendrecv" });
    const videoTransceiver = state.pc.addTransceiver("video", { direction: "sendrecv" });
    state.dc = state.pc.createDataChannel("chat", { ordered: true });
    wireDataChannel();

    const audioTrack = state.localStream.getAudioTracks()[0];
    const videoTrack = state.localStream.getVideoTracks()[0];
    if (audioTrack) await audioTransceiver.sender.replaceTrack(audioTrack);
    if (videoTrack) await videoTransceiver.sender.replaceTrack(videoTrack);

    await negotiate(false);
    setStatus("Connecting");
    addUniqueMessage("system", "Media connected. Waiting for live transcript...");
  } catch (error) {
    addUniqueMessage("system", error.message || String(error));
    await cleanup({ keepMedia: true });
    showLobby();
    setPreviewStatus(error.message || "Could not join call", false);
    setStatus("Error");
    setButtons({ connected: false });
  }
}

function wirePeerConnection() {
  state.pc.addEventListener("icecandidate", (event) => {
    if (event.candidate) {
      queueIceCandidate(event.candidate);
    }
  });

  state.pc.addEventListener("track", (event) => {
    const [stream] = event.streams;
    if (event.track.kind === "audio") {
      elements.agentAudio.srcObject = stream || new MediaStream([event.track]);
      elements.agentAudio.muted = false;
      elements.agentAudio.volume = 1;
      elements.agentAudioStatus.textContent = "Connected";
      void applySpeakerSelection();
      void ensureAgentAudioPlaying("remote audio track");
      startAgentAudioMonitor();
    }
    if (event.track.kind === "video") {
      elements.agentVideo.srcObject = stream || new MediaStream([event.track]);
      elements.stageEmpty.classList.remove("hidden");
      elements.agentVideo.classList.add("pending");
      elements.agentVideo.play().catch(() => {});
      startAgentVideoMonitor();
    }
  });

  state.pc.addEventListener("connectionstatechange", () => {
    const pcState = state.pc?.connectionState || "closed";
    if (pcState === "connected") {
      state.connected = true;
      setStatus(state.dataChannelOpen ? "Live" : "Media live", true);
      setButtons({ connected: true });
    } else if (["failed", "closed", "disconnected"].includes(pcState)) {
      setStatus(pcState);
      setButtons({ connected: false });
    } else {
      setStatus(pcState);
    }
  });
}

function wireDataChannel() {
  state.dc.addEventListener("open", () => {
    state.dataChannelOpen = true;
    syncTrackStatus();
    sendRTVI("client-ready", {
      version: RTVI_VERSION,
      about: { library: "bayview-video-demo", library_version: "0.1.0" },
    });
    state.pingTimer = window.setInterval(() => {
      if (state.dc?.readyState === "open") state.dc.send(`ping: ${Date.now()}`);
    }, 1000);
    setStatus("Live", true);
    setButtons({ connected: true });
    elements.sendButton.disabled = false;
    addUniqueMessage("system", "Transcript connected.");
    flushVisualObservation();
    flushGestureLanguageRequest();
  });

  state.dc.addEventListener("message", async (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }

    if (message.type === "signalling" && message.message?.type === "renegotiate") {
      await negotiate(false);
      return;
    }

    if (message.type === "signalling" && message.message?.type === "peerLeft") {
      addUniqueMessage("system", "Agent disconnected.");
      await cleanup();
      showLobby();
      void initPreview();
      return;
    }

    if (message.label === "rtvi-ai") {
      handleRTVIMessage(message);
    }
  });

  state.dc.addEventListener("close", () => {
    state.dataChannelOpen = false;
    elements.sendButton.disabled = true;
    if (state.pingTimer) window.clearInterval(state.pingTimer);
    state.pingTimer = null;
  });
}

async function getLocalMedia() {
  const audio = selectedDeviceConstraint(elements.micSelect);
  const video = {
    ...selectedDeviceConstraint(elements.cameraSelect),
    width: { ideal: 1280 },
    height: { ideal: 720 },
  };

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio, video });
    applyLocalMediaPreferences(stream);
    return stream;
  } catch {
    addUniqueMessage("system", "Camera unavailable; joining with microphone only.");
    const stream = await navigator.mediaDevices.getUserMedia({ audio, video: false });
    applyLocalMediaPreferences(stream);
    return stream;
  }
}

function applyLocalMediaPreferences(stream) {
  const [audioTrack] = stream.getAudioTracks();
  const [videoTrack] = stream.getVideoTracks();
  if (audioTrack) audioTrack.enabled = !state.muted;
  if (videoTrack) videoTrack.enabled = state.cameraEnabled;
}

function selectedDeviceConstraint(select) {
  return select.value ? { deviceId: { exact: select.value } } : true;
}

function syncDeviceSelects(kind, value) {
  if (kind === "audio") {
    elements.micSelect.value = value;
    elements.callMicSelect.value = value;
  } else if (kind === "video") {
    elements.cameraSelect.value = value;
    elements.callCameraSelect.value = value;
  } else if (kind === "speaker") {
    elements.speakerSelect.value = value;
    elements.callSpeakerSelect.value = value;
  }
}

async function handleDeviceSelect(kind, value) {
  syncDeviceSelects(kind, value);
  if (kind === "speaker") {
    await applySpeakerSelection();
    return;
  }
  await replaceInputTrack(kind);
}

function assignLocalStream(stream) {
  elements.previewVideo.srcObject = stream;
  elements.selfView.srcObject = stream;
  updatePreviewVideoState();
  setPreviewMediaButtons();
  setCallMediaState();
}

async function negotiate(restartPc) {
  const offer = await state.pc.createOffer({ iceRestart: restartPc });
  await state.pc.setLocalDescription(offer);
  await waitForIceGatheringComplete();

  const response = await fetch(`/sessions/${state.sessionId}/api/offer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      sdp: state.pc.localDescription.sdp,
      type: state.pc.localDescription.type,
      pc_id: state.pcId,
      restart_pc: restartPc,
      request_data: { client: "bayview-video-demo" },
    }),
  });

  if (!response.ok) {
    throw new Error(`Offer failed: ${response.status}`);
  }

  const answer = await response.json();
  state.pcId = answer.pc_id;
  await state.pc.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
  state.canSendIceCandidates = true;
  await flushIceCandidates();
}

function waitForIceGatheringComplete() {
  if (state.pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const timeout = window.setTimeout(done, 2000);
    state.pc.addEventListener("icegatheringstatechange", onStateChange);

    function onStateChange() {
      if (state.pc.iceGatheringState === "complete") done();
    }

    function done() {
      window.clearTimeout(timeout);
      state.pc.removeEventListener("icegatheringstatechange", onStateChange);
      resolve();
    }
  });
}

function queueIceCandidate(candidate) {
  state.pendingCandidates.push(candidate);
  if (state.iceFlushTimer) return;
  state.iceFlushTimer = window.setTimeout(() => void flushIceCandidates(), 200);
}

async function flushIceCandidates() {
  if (state.iceFlushTimer) {
    window.clearTimeout(state.iceFlushTimer);
    state.iceFlushTimer = null;
  }
  if (!state.canSendIceCandidates || !state.pcId || state.pendingCandidates.length === 0) return;

  const candidates = state.pendingCandidates.splice(0, state.pendingCandidates.length);
  await fetch(`/sessions/${state.sessionId}/api/offer`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      pc_id: state.pcId,
      candidates: candidates.map((candidate) => ({
        candidate: candidate.candidate,
        sdp_mid: candidate.sdpMid,
        sdp_mline_index: candidate.sdpMLineIndex,
      })),
    }),
  });
}

function syncTrackStatus() {
  const audioTrack = state.localStream?.getAudioTracks()[0];
  const videoTrack = state.localStream?.getVideoTracks()[0];
  sendSignalling("trackStatus", { receiver_index: 0, enabled: Boolean(audioTrack?.enabled) });
  sendSignalling("trackStatus", { receiver_index: 1, enabled: Boolean(videoTrack?.enabled) });
}

function sendSignalling(type, payload) {
  if (state.dc?.readyState !== "open") return;
  state.dc.send(JSON.stringify({ type: "signalling", message: { type, ...payload } }));
}

function sendRTVI(type, data) {
  if (state.dc?.readyState !== "open") return false;
  state.dc.send(
    JSON.stringify({
      label: "rtvi-ai",
      type,
      data,
      id: Math.random().toString(16).slice(2, 10),
    })
  );
  return true;
}

function handleRTVIMessage(message) {
  const text = cleanTranscriptText(message.data?.text);
  if (message.type === "bot-output" && text && message.data?.spoken && !isWordLevel(message)) {
    addUniqueMessage("agent", text);
  } else if (message.type === "bot-transcription" && text) {
    addUniqueMessage("agent", text);
  } else if (message.type === "user-transcription" && message.data?.final && text) {
    addUniqueMessage("user", text);
  } else if (message.type === "user-llm-text" && text && !isInternalKickoffText(text)) {
    addUniqueMessage("user", text);
  } else if (message.type === "bot-ready") {
    setStatus("Live", true);
    addUniqueMessage("system", "Agent ready.");
  } else if (message.type === "bot-started-speaking") {
    setAgentSpeaking(true);
  } else if (message.type === "bot-stopped-speaking") {
    setAgentSpeaking(false);
  } else if (message.type === "user-started-speaking") {
    state.userSpeakingFromRTVI = true;
    updateUserSpeaker();
  } else if (message.type === "user-stopped-speaking") {
    state.userSpeakingFromRTVI = false;
    updateUserSpeaker();
  } else if (message.type === "error" || message.type === "error-response") {
    addUniqueMessage("system", message.data?.message || message.data?.error || "Agent error");
  }
}

function cleanTranscriptText(text) {
  return typeof text === "string" ? text.replace(/^[○✓◐]\s*/, "").trim() : "";
}

function isWordLevel(message) {
  return String(message.data?.aggregated_by || "").toLowerCase() === "word";
}

function isInternalKickoffText(text) {
  return (
    text.startsWith("A caller just connected.") ||
    text.startsWith("VISUAL_CONTEXT:") ||
    text.startsWith("GESTURE_CONTEXT:")
  );
}

function toggleMute() {
  const audioTrack = state.localStream?.getAudioTracks()[0];
  if (!audioTrack) return;
  state.muted = !state.muted;
  audioTrack.enabled = !state.muted;
  sendSignalling("trackStatus", { receiver_index: 0, enabled: audioTrack.enabled });
  elements.muteButton.textContent = state.muted ? "Unmute" : "Mute";
  setPreviewMediaButtons();
  setCallMediaState();
  updateUserSpeaker();
}

function togglePreviewMic() {
  const audioTrack = state.localStream?.getAudioTracks()[0];
  if (!audioTrack) return;
  state.muted = audioTrack.enabled;
  audioTrack.enabled = !state.muted;
  if (state.connected) sendSignalling("trackStatus", { receiver_index: 0, enabled: audioTrack.enabled });
  elements.muteButton.textContent = state.muted ? "Unmute" : "Mute";
  setPreviewMediaButtons();
  setCallMediaState();
  updateUserSpeaker();
}

function togglePreviewCamera() {
  const videoTrack = state.localStream?.getVideoTracks()[0];
  state.cameraEnabled = !state.cameraEnabled;
  if (videoTrack) videoTrack.enabled = state.cameraEnabled;
  if (state.connected) sendSignalling("trackStatus", { receiver_index: 1, enabled: Boolean(videoTrack?.enabled) });
  updatePreviewVideoState();
  setPreviewMediaButtons();
  setCallMediaState();
  if (state.cameraEnabled) {
    void startVisualDetection();
    void startGestureDetection();
  } else {
    stopVisualDetection();
    stopGestureDetection();
    setVisualBadge("idle", "Camera off");
    setGestureBadge("idle", "Camera off");
  }
}

async function sendChatMessage(event) {
  event.preventDefault();
  const text = elements.chatInput.value.trim();
  if (!text) return;
  if (!sendRTVI("send-text", {
    content: text,
    options: { run_immediately: true, audio_response: true },
  })) {
    addUniqueMessage("system", "Chat is available after the call connects.");
    return;
  }
  elements.chatInput.value = "";
  addUniqueMessage("user", text);
}

async function replaceInputTrack(kind) {
  const isAudio = kind === "audio";
  const constraints = isAudio
    ? { audio: selectedDeviceConstraint(elements.micSelect), video: false }
    : { audio: false, video: selectedDeviceConstraint(elements.cameraSelect) };

  try {
    const stream = await navigator.mediaDevices.getUserMedia(constraints);
    const [newTrack] = isAudio ? stream.getAudioTracks() : stream.getVideoTracks();
    const [oldTrack] = isAudio
      ? state.localStream?.getAudioTracks() || []
      : state.localStream?.getVideoTracks() || [];
    const transceiver = state.pc?.getTransceivers()[isAudio ? 0 : 1];
    if (newTrack) newTrack.enabled = isAudio ? !state.muted : state.cameraEnabled;
    if (state.connected) await transceiver?.sender.replaceTrack(newTrack || null);

    if (!state.localStream) {
      state.localStream = new MediaStream();
    }
    if (oldTrack) {
      state.localStream.removeTrack(oldTrack);
      oldTrack.stop();
    }
    if (newTrack) state.localStream.addTrack(newTrack);
    assignLocalStream(state.localStream);
    if (isAudio) await startMicMeter(state.localStream);
    if (!isAudio) {
      void startVisualDetection();
      void startGestureDetection();
    }
    if (state.connected) syncTrackStatus();
  } catch (error) {
    addUniqueMessage("system", error.message || String(error));
  }
}

async function refreshDevices() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  fillDeviceSelectGroup(
    [elements.micSelect, elements.callMicSelect],
    devices,
    "audioinput",
    "Default microphone"
  );
  fillDeviceSelectGroup(
    [elements.cameraSelect, elements.callCameraSelect],
    devices,
    "videoinput",
    "Default camera"
  );
  fillDeviceSelectGroup(
    [elements.speakerSelect, elements.callSpeakerSelect],
    devices,
    "audiooutput",
    "Default speaker"
  );
  const speakerSelectionSupported = "setSinkId" in HTMLMediaElement.prototype;
  elements.speakerSelect.disabled = !speakerSelectionSupported;
  elements.callSpeakerSelect.disabled = !speakerSelectionSupported;
}

function fillDeviceSelectGroup(selects, devices, kind, fallbackLabel) {
  const currentValue = selects.find((select) => select.value)?.value || "";
  for (const select of selects) {
    fillDeviceSelect(select, devices, kind, fallbackLabel, currentValue);
  }
}

function fillDeviceSelect(select, devices, kind, fallbackLabel, preferredValue = select.value) {
  const options = devices.filter((device) => device.kind === kind);
  select.replaceChildren(new Option(fallbackLabel, ""));
  for (const [index, device] of options.entries()) {
    select.append(new Option(device.label || `${fallbackLabel} ${index + 1}`, device.deviceId));
  }
  if ([...select.options].some((option) => option.value === preferredValue)) {
    select.value = preferredValue;
  }
}

async function applySpeakerSelection() {
  if (!("setSinkId" in elements.agentAudio)) return;
  try {
    await elements.agentAudio.setSinkId(elements.speakerSelect.value);
    if (elements.agentAudio.srcObject) void ensureAgentAudioPlaying("speaker changed");
  } catch (error) {
    elements.agentAudioStatus.textContent = "Speaker blocked";
    addUniqueMessage("system", error.message || "Could not switch speaker output.");
  }
}

async function requestSpeakerOutput() {
  if (!navigator.mediaDevices?.selectAudioOutput) {
    elements.agentAudioStatus.textContent = "Use system output";
    addUniqueMessage("system", "This browser does not support choosing a speaker here. Set AirPods as the system output.");
    return;
  }

  try {
    const device = await navigator.mediaDevices.selectAudioOutput({
      deviceId: elements.speakerSelect.value || undefined,
    });
    await refreshDevices();
    syncDeviceSelects("speaker", device.deviceId);
    await applySpeakerSelection();
    if (elements.agentAudio.srcObject) {
      await ensureAgentAudioPlaying("speaker permission selected");
    }
    elements.agentAudioStatus.textContent = device.label ? `Output: ${device.label}` : "Output selected";
  } catch (error) {
    elements.agentAudioStatus.textContent = "Output blocked";
    addUniqueMessage("system", error.message || "Speaker output selection was cancelled.");
  }
}

async function endCall() {
  await cleanup();
  showLobby();
  setPreviewStatus("Call ended", true);
  setStatus("Idle");
  setButtons({ connected: false });
  void initPreview();
}

async function cleanup({ keepMedia = false } = {}) {
  if (state.pingTimer) window.clearInterval(state.pingTimer);
  if (state.iceFlushTimer) window.clearTimeout(state.iceFlushTimer);
  if (state.agentVideoCheckTimer) window.clearTimeout(state.agentVideoCheckTimer);
  if (state.agentAudioRetryTimer) window.clearTimeout(state.agentAudioRetryTimer);
  if (state.micHealthTimer) window.clearTimeout(state.micHealthTimer);
  if (state.avatarConfigTimer) window.clearInterval(state.avatarConfigTimer);
  state.pingTimer = null;
  state.iceFlushTimer = null;
  state.agentVideoCheckTimer = null;
  state.agentAudioRetryTimer = null;
  state.micHealthTimer = null;
  state.avatarConfigTimer = null;
  if (state.dc && state.dc.readyState !== "closed") state.dc.close();
  if (state.pc) state.pc.close();
  if (!keepMedia) {
    if (state.localStream) {
      for (const track of state.localStream.getTracks()) track.stop();
    }
    state.localStream = null;
    elements.previewVideo.srcObject = null;
    elements.selfView.srcObject = null;
    stopVisualDetection();
    stopGestureDetection();
    stopMicMeter();
  }
  elements.agentAudio.srcObject = null;
  elements.agentVideo.srcObject = null;
  elements.agentAudioStatus.textContent = "Waiting";
  elements.audioRetryButton.hidden = true;
  elements.agentVideo.classList.remove("pending");
  elements.muteButton.textContent = "Mute";
  elements.sendButton.disabled = true;
  elements.stageEmpty.classList.remove("hidden");
  setAgentSpeaking(false);
  state.userSpeakingFromRTVI = false;
  state.userSpeakingFromMeter = false;
  updateUserSpeaker();
  Object.assign(state, {
    pc: null,
    dc: null,
    sessionId: null,
    pcId: null,
    pingTimer: null,
    iceFlushTimer: null,
    micHealthTimer: null,
    avatarConfigTimer: null,
    pendingCandidates: [],
    canSendIceCandidates: false,
    muted: false,
    connected: false,
    dataChannelOpen: false,
    visualObservationPending: false,
    visualObservationSent: false,
    visualCueMessageShown: false,
    gestureLanguagePending: false,
    gestureLanguageSent: false,
    gestureCueMessageShown: false,
    activeLanguage: "English",
  });
  setVisualBadge("idle", "Visual detection ready");
  setGestureBadge("idle", "Gesture detection ready");
}

function showCall() {
  elements.prejoinShell.classList.add("hidden");
  elements.callShell.classList.remove("hidden");
}

function showLobby() {
  elements.callShell.classList.add("hidden");
  elements.prejoinShell.classList.remove("hidden");
  elements.joinButton.disabled = false;
  if (state.avatarConfigTimer) window.clearInterval(state.avatarConfigTimer);
  state.avatarConfigTimer = null;
}

function createMeterBars(container) {
  container.replaceChildren();
  for (let index = 0; index < 12; index += 1) {
    container.append(document.createElement("span"));
  }
}

function setPreviewMediaButtons() {
  const audioTrack = state.localStream?.getAudioTracks()[0];
  const videoTrack = state.localStream?.getVideoTracks()[0];
  const micOn = Boolean(audioTrack?.enabled);
  const cameraOn = Boolean(videoTrack?.enabled);
  elements.previewMuteButton.textContent = micOn ? "Mic" : "Mic off";
  elements.previewMuteButton.classList.toggle("off", !micOn);
  elements.previewMuteButton.disabled = !audioTrack;
  elements.previewCameraButton.textContent = cameraOn ? "Cam" : "Cam off";
  elements.previewCameraButton.classList.toggle("off", !cameraOn);
  elements.previewCameraButton.disabled = !videoTrack;
}

function setCallMediaState() {
  const audioTrack = state.localStream?.getAudioTracks()[0];
  const videoTrack = state.localStream?.getVideoTracks()[0];
  const micOn = Boolean(audioTrack?.enabled);
  const cameraOn = Boolean(videoTrack?.enabled);
  elements.muteButton.textContent = micOn ? "Mute" : "Unmute";
  elements.userSpeakerText.textContent = !micOn ? "Muted" : "Mic active";
  elements.callMicSelect.classList.toggle("track-off", !micOn);
  elements.callCameraSelect.classList.toggle("track-off", !cameraOn);
}

function ensureMicEnabledForJoin() {
  const audioTrack = state.localStream?.getAudioTracks()[0];
  if (!audioTrack) return;
  state.muted = false;
  audioTrack.enabled = true;
  setPreviewMediaButtons();
  setCallMediaState();
}

async function resumeMicMeter() {
  if (state.audioContext?.state === "suspended") {
    try {
      await state.audioContext.resume();
    } catch {
      // The next user gesture or track replacement will retry the meter.
    }
  }
}

function startMicHealthMonitor() {
  if (state.micHealthTimer) window.clearTimeout(state.micHealthTimer);

  const checkMic = async () => {
    if (!state.pc) return;
    const audioTrack = state.localStream?.getAudioTracks()[0];

    if (!audioTrack || audioTrack.readyState !== "live") {
      await replaceInputTrack("audio");
    } else if (!state.muted && !audioTrack.enabled) {
      audioTrack.enabled = true;
      syncTrackStatus();
      setPreviewMediaButtons();
      setCallMediaState();
    }

    await resumeMicMeter();
    state.micHealthTimer = window.setTimeout(checkMic, 2000);
  };

  state.micHealthTimer = window.setTimeout(checkMic, 1000);
}

function unlockAgentAudio() {
  elements.agentAudio.autoplay = true;
  elements.agentAudio.muted = false;
  elements.agentAudio.volume = 1;
  elements.audioRetryButton.hidden = true;
  elements.agentAudioStatus.textContent = "Waiting";
}

async function ensureAgentAudioPlaying(reason) {
  if (!elements.agentAudio.srcObject) return;
  elements.agentAudio.muted = false;
  elements.agentAudio.volume = 1;

  try {
    await elements.agentAudio.play();
    elements.agentAudioStatus.textContent = "Playing";
    elements.audioRetryButton.hidden = true;
  } catch {
    elements.agentAudioStatus.textContent = "Click play";
    elements.audioRetryButton.hidden = false;
  }

  window.__bayviewLastAudioPlayReason = reason;
}

function startAgentAudioMonitor() {
  if (state.agentAudioRetryTimer) window.clearTimeout(state.agentAudioRetryTimer);

  const checkAudio = () => {
    if (!elements.agentAudio.srcObject || !state.pc) return;
    if (elements.agentAudio.paused) {
      void ensureAgentAudioPlaying("monitor retry");
    } else if (elements.agentAudio.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
      elements.agentAudioStatus.textContent = "Playing";
      elements.audioRetryButton.hidden = true;
    }
    state.agentAudioRetryTimer = window.setTimeout(checkAudio, 1500);
  };

  state.agentAudioRetryTimer = window.setTimeout(checkAudio, 500);
}

function updatePreviewVideoState() {
  const videoTrack = state.localStream?.getVideoTracks()[0];
  const showCamera = Boolean(videoTrack?.enabled);
  elements.previewVideo.classList.toggle("disabled", !showCamera);
  elements.previewEmpty.classList.toggle("hidden", showCamera);
}

async function startVisualDetection() {
  if (!state.localStream?.getVideoTracks()[0] || !state.cameraEnabled) {
    setVisualBadge("idle", "Camera off");
    return;
  }

  try {
    await loadVisualDetector();
    if (!state.visualDetector || state.visualDetectionTimer) return;
    setVisualBadge("ready", "Watching for prescription bottle cue");
    state.visualDetectionTimer = window.setTimeout(runVisualDetection, 250);
  } catch {
    setVisualBadge("error", "Visual detection unavailable");
  }
}

async function loadVisualDetector() {
  if (state.visualDetector) return state.visualDetector;
  if (!state.visualDetectorPromise) {
    state.visualDetectorLoading = true;
    setVisualBadge("idle", "Visual detection loading");
    state.visualDetectorPromise = import(MEDIAPIPE_MODULE_URL).then(
      async ({ ObjectDetector }) => {
        const vision = await loadVisionFileset();
        return ObjectDetector.createFromOptions(vision, {
          baseOptions: {
            modelAssetPath: MEDIAPIPE_MODEL_URL,
          },
          runningMode: "VIDEO",
          scoreThreshold: 0.25,
          maxResults: 5,
        });
      }
    );
  }

  try {
    state.visualDetector = await state.visualDetectorPromise;
    return state.visualDetector;
  } finally {
    state.visualDetectorLoading = false;
  }
}

async function loadVisionFileset() {
  if (!state.visionFilesetPromise) {
    state.visionFilesetPromise = import(MEDIAPIPE_MODULE_URL).then(({ FilesetResolver }) =>
      FilesetResolver.forVisionTasks(MEDIAPIPE_WASM_ROOT)
    );
  }
  return state.visionFilesetPromise;
}

function stopVisualDetection() {
  if (state.visualDetectionTimer) window.clearTimeout(state.visualDetectionTimer);
  state.visualDetectionTimer = null;
  state.visualDetectionHits = [];
  hideDetectionBox();
}

function runVisualDetection() {
  state.visualDetectionTimer = null;

  const video = getVisualDetectionVideo();
  if (!state.visualDetector || !state.cameraEnabled || !video?.srcObject) {
    return;
  }

  const now = performance.now();
  if (
    video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA &&
    video.videoWidth > 0 &&
    now - state.visualLastRunAt >= VISUAL_DETECTION_INTERVAL_MS
  ) {
    state.visualLastRunAt = now;
    const source = drawDetectionFrame(video);
    if (source) {
      const result = state.visualDetector.detectForVideo(source.canvas, now);
      handleVisualDetections(result, video, source);
    }
  }

  state.visualDetectionTimer = window.setTimeout(runVisualDetection, VISUAL_DETECTION_INTERVAL_MS);
}

function getVisualDetectionVideo() {
  return elements.callShell.classList.contains("hidden") ? elements.previewVideo : elements.selfView;
}

function drawDetectionFrame(video) {
  if (!state.visualCanvas) {
    state.visualCanvas = document.createElement("canvas");
    state.visualCanvasContext = state.visualCanvas.getContext("2d", { alpha: false });
  }
  if (!state.visualCanvasContext || !video.videoWidth || !video.videoHeight) return null;

  const width = 320;
  const height = Math.round(width * (video.videoHeight / video.videoWidth));
  if (state.visualCanvas.width !== width || state.visualCanvas.height !== height) {
    state.visualCanvas.width = width;
    state.visualCanvas.height = height;
  }
  state.visualCanvasContext.drawImage(video, 0, 0, width, height);
  return { canvas: state.visualCanvas, width, height };
}

function handleVisualDetections(result, video, source) {
  state.visualLastDetections = summarizeVisualDetections(result);
  const detection = findVisualDemoDetection(result);
  if (!detection) {
    hideDetectionBox();
    if (!state.visualObservationPending && !state.visualObservationSent) {
      setVisualBadge("ready", "Watching for prescription bottle cue");
    }
    return;
  }

  updateDetectionBox(detection.boundingBox, video, source);

  const now = Date.now();
  state.visualDetectionHits.push(now);
  state.visualDetectionHits = state.visualDetectionHits.filter(
    (timestamp) => now - timestamp <= VISUAL_DETECTION_WINDOW_MS
  );

  if (state.visualDetectionHits.length >= VISUAL_DETECTION_REQUIRED_HITS) {
    handlePrescriptionBottleCue();
  } else {
    setVisualBadge("ready", "Possible prescription bottle cue");
  }
}

function summarizeVisualDetections(result) {
  return (result?.detections || []).slice(0, 5).map((detection) => {
    const category = detection.categories?.[0];
    return {
      label: category?.categoryName || category?.displayName || "",
      score: Number(category?.score || 0).toFixed(2),
    };
  });
}

function findVisualDemoDetection(result) {
  for (const detection of result?.detections || []) {
    const category = detection.categories?.find(
      (item) => {
        const label = `${item.categoryName || ""} ${item.displayName || ""}`.toLowerCase();
        return (
          VISUAL_DEMO_CATEGORIES.some((categoryName) => label.includes(categoryName)) &&
          item.score >= VISUAL_DETECTION_THRESHOLD
        );
      }
    );
    if (category) {
      return {
        boundingBox: detection.boundingBox,
        score: category.score,
      };
    }
  }
  return null;
}

function updateDetectionBox(box, video, source) {
  if (!box || !video.videoWidth || !video.videoHeight) return;
  const detectionBox =
    video === elements.selfView ? elements.callDetectionBox : elements.previewDetectionBox;
  const sourceWidth = source?.width || video.videoWidth;
  const sourceHeight = source?.height || video.videoHeight;
  const left = (box.originX / sourceWidth) * 100;
  const top = (box.originY / sourceHeight) * 100;
  const width = (box.width / sourceWidth) * 100;
  const height = (box.height / sourceHeight) * 100;

  detectionBox.style.left = `${left}%`;
  detectionBox.style.top = `${top}%`;
  detectionBox.style.width = `${width}%`;
  detectionBox.style.height = `${height}%`;
  detectionBox.classList.remove("hidden");
}

function hideDetectionBox() {
  elements.previewDetectionBox.classList.add("hidden");
  elements.callDetectionBox.classList.add("hidden");
}

function handlePrescriptionBottleCue() {
  state.visualObservationPending = true;
  setVisualBadge("detected", "Prescription bottle detected");
  if (state.connected && !state.visualCueMessageShown) {
    addUniqueMessage("system", "Visual cue: prescription bottle detected (demo).");
    state.visualCueMessageShown = true;
  }
  flushVisualObservation();
}

function flushVisualObservation() {
  if (!state.visualObservationPending || state.visualObservationSent || state.dc?.readyState !== "open") {
    return;
  }

  const sent = sendRTVI("send-text", {
    content:
      "VISUAL_CONTEXT: For this hackathon demo, the caller's camera detected a cup or bottle. Treat that visual cue as an empty prescription bottle. Ask one brief confirmation question about whether they are calling for a refill. Do not mention that a cup was detected, and do not claim certainty about the image.",
    options: { run_immediately: true, audio_response: true },
  });

  if (sent) {
    state.visualObservationPending = false;
    state.visualObservationSent = true;
    if (!state.visualCueMessageShown) {
      addUniqueMessage("system", "Visual cue: prescription bottle detected (demo).");
      state.visualCueMessageShown = true;
    }
  }
}

async function startGestureDetection() {
  if (!state.localStream?.getVideoTracks()[0] || !state.cameraEnabled) {
    setGestureBadge("idle", "Camera off");
    return;
  }

  try {
    await loadGestureRecognizer();
    if (!state.gestureRecognizer || state.gestureDetectionTimer) return;
    setGestureBadge("ready", "1 English / 2 Spanish");
    state.gestureDetectionTimer = window.setTimeout(runGestureDetection, 300);
  } catch {
    setGestureBadge("error", "Gesture detection unavailable");
  }
}

async function loadGestureRecognizer() {
  if (state.gestureRecognizer) return state.gestureRecognizer;
  if (!state.gestureRecognizerPromise) {
    state.gestureRecognizerLoading = true;
    setGestureBadge("idle", "Gesture detection loading");
    state.gestureRecognizerPromise = import(MEDIAPIPE_MODULE_URL).then(
      async ({ GestureRecognizer }) => {
        const vision = await loadVisionFileset();
        return GestureRecognizer.createFromOptions(vision, {
          baseOptions: {
            modelAssetPath: MEDIAPIPE_GESTURE_MODEL_URL,
          },
          runningMode: "VIDEO",
          numHands: 1,
          minHandDetectionConfidence: 0.45,
          minHandPresenceConfidence: 0.45,
          minTrackingConfidence: 0.45,
        });
      }
    );
  }

  try {
    state.gestureRecognizer = await state.gestureRecognizerPromise;
    return state.gestureRecognizer;
  } finally {
    state.gestureRecognizerLoading = false;
  }
}

function stopGestureDetection() {
  if (state.gestureDetectionTimer) window.clearTimeout(state.gestureDetectionTimer);
  state.gestureDetectionTimer = null;
  state.gestureDetectionHits = [];
}

function runGestureDetection() {
  state.gestureDetectionTimer = null;

  const video = getVisualDetectionVideo();
  if (!state.gestureRecognizer || !state.cameraEnabled || !video?.srcObject) {
    return;
  }

  const now = performance.now();
  if (
    video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA &&
    video.videoWidth > 0 &&
    now - state.gestureLastRunAt >= GESTURE_DETECTION_INTERVAL_MS
  ) {
    state.gestureLastRunAt = now;
    const source = drawDetectionFrame(video);
    if (source) {
      const result = state.gestureRecognizer.recognizeForVideo(source.canvas, now);
      handleGestureRecognition(result);
    }
  }

  state.gestureDetectionTimer = window.setTimeout(
    runGestureDetection,
    GESTURE_DETECTION_INTERVAL_MS
  );
}

function handleGestureRecognition(result) {
  const gesture = result?.gestures?.[0]?.[0];
  const label = gesture?.categoryName || "";
  const score = Number(gesture?.score || 0);
  state.gestureLastResult = label ? { label, score: score.toFixed(2) } : null;
  if (state.gestureLanguageSent) return;

  const languageCue = GESTURE_LANGUAGE_MAP[label];
  if (!languageCue || score < GESTURE_DETECTION_THRESHOLD) {
    if (!state.gestureLanguagePending && !state.gestureLanguageSent) {
      setGestureBadge("ready", "1 English / 2 Spanish");
    }
    return;
  }

  const now = Date.now();
  state.gestureDetectionHits.push(now);
  state.gestureDetectionHits = state.gestureDetectionHits.filter(
    (timestamp) => now - timestamp <= GESTURE_DETECTION_WINDOW_MS
  );

  if (state.gestureDetectionHits.length >= GESTURE_DETECTION_REQUIRED_HITS) {
    handleLanguageGestureCue(languageCue, label, score);
  } else {
    setGestureBadge("ready", `${languageCue.language} gesture seen`);
  }
}

function handleLanguageGestureCue(languageCue, label, score) {
  state.activeLanguage = languageCue.language;
  state.gestureLanguagePending = true;
  setGestureBadge("detected", `${languageCue.language} requested`);
  if (state.connected && !state.gestureCueMessageShown) {
    addUniqueMessage("system", `Gesture cue: ${languageCue.language} requested.`);
    state.gestureCueMessageShown = true;
  }
  window.__bayviewLastLanguageGesture = { ...languageCue, label, score };
  flushGestureLanguageRequest();
}

function flushGestureLanguageRequest() {
  if (!state.gestureLanguagePending || state.gestureLanguageSent || state.dc?.readyState !== "open") {
    return;
  }

  const sent = sendRTVI("send-text", {
    content:
      `GESTURE_CONTEXT: The caller selected ${state.activeLanguage} with the hand gesture. This answers your language preference question. Continue in ${state.activeLanguage} unless the caller asks to switch back. Say one brief confirmation in ${state.activeLanguage}, then ask how you can help today in ${state.activeLanguage}.`,
    options: { run_immediately: true, audio_response: true },
  });

  if (sent) {
    state.gestureLanguagePending = false;
    state.gestureLanguageSent = true;
    if (!state.gestureCueMessageShown) {
      addUniqueMessage("system", `Gesture cue: ${state.activeLanguage} requested.`);
      state.gestureCueMessageShown = true;
    }
  }
}

function setVisualBadge(status, text) {
  for (const badge of [elements.previewVisualBadge, elements.callVisualBadge]) {
    badge.classList.remove("idle", "ready", "detected", "error");
    badge.classList.add(status);
    badge.lastElementChild.textContent = text;
  }
}

function setGestureBadge(status, text) {
  for (const badge of [elements.previewGestureBadge, elements.callGestureBadge]) {
    badge.classList.remove("idle", "ready", "detected", "error");
    badge.classList.add(status);
    badge.lastElementChild.textContent = text;
  }
}

function setAvatarStatus(avatar) {
  let text = "AI Avatar audio-only";
  let live = false;
  let warning = false;
  const status = avatar?.status || "";

  if (avatar?.error) {
    text = "AI Avatar config error";
    warning = true;
  } else if (avatar?.enabled && !avatar?.configured) {
    text = "AI Avatar setup needed";
    warning = true;
  } else if (status === "starting") {
    text = "AI Avatar starting";
  } else if (["ready", "room_joined", "avatar_joined"].includes(status)) {
    text = "AI Avatar joining";
  } else if (status === "video_active") {
    text = "AI Avatar live";
    live = true;
  } else if (status === "unavailable" || status === "error") {
    text = "AI Avatar unavailable";
    warning = true;
  } else if (avatar?.enabled && avatar?.configured) {
    text = "AI Avatar configured";
  }

  elements.avatarStatus.classList.toggle("warning", warning);
  elements.avatarStatus.querySelector(".status-dot").classList.toggle("live", live);
  elements.avatarStatus.querySelector(".status-dot").classList.toggle("warning", warning);
  elements.avatarStatus.lastElementChild.textContent = text;
  const envIssueTitle = avatar?.env_issues?.length
    ? avatar.env_issues.map((issue) => `${issue.name} ${issue.reason}`).join(", ")
    : "";
  elements.avatarStatus.title = envIssueTitle || avatar?.last_error || avatar?.message || "";

  elements.callAvatarStatus.classList.toggle("ready", live);
  elements.callAvatarStatus.classList.toggle("warning", warning);
  elements.callAvatarStatus.textContent = text;
  elements.callAvatarStatus.title = elements.avatarStatus.title;
}

function startAvatarStatusPolling() {
  if (state.avatarConfigTimer) return;
  state.avatarConfigTimer = window.setInterval(() => void loadDemoConfig(), 1500);
}

window.__bayviewVisualState = () => ({
  detectorLoaded: Boolean(state.visualDetector),
  detectorLoading: state.visualDetectorLoading,
  detectingOn: elements.callShell.classList.contains("hidden") ? "preview" : "self-view",
  lastDetections: state.visualLastDetections,
  pending: state.visualObservationPending,
  sent: state.visualObservationSent,
});
window.__bayviewTriggerVisualCue = () => handlePrescriptionBottleCue();
window.__bayviewGestureState = () => ({
  recognizerLoaded: Boolean(state.gestureRecognizer),
  recognizerLoading: state.gestureRecognizerLoading,
  detectingOn: elements.callShell.classList.contains("hidden") ? "preview" : "self-view",
  lastGesture: state.gestureLastResult,
  activeLanguage: state.activeLanguage,
  pending: state.gestureLanguagePending,
  sent: state.gestureLanguageSent,
});
window.__bayviewTriggerSpanishGesture = () =>
  handleLanguageGestureCue({ language: "Spanish", code: "es" }, "debug", 1);
window.__bayviewTriggerEnglishGesture = () =>
  handleLanguageGestureCue({ language: "English", code: "en" }, "debug", 1);
window.__bayviewAudioState = () => {
  const audioTrack = state.localStream?.getAudioTracks()[0];
  const remoteTrack = elements.agentAudio.srcObject?.getAudioTracks?.()[0];
  return {
    localMicEnabled: Boolean(audioTrack?.enabled),
    localMicMuted: Boolean(audioTrack?.muted),
    localMicReadyState: audioTrack?.readyState || null,
    mutedState: state.muted,
    agentAudioPaused: elements.agentAudio.paused,
    agentAudioMuted: elements.agentAudio.muted,
    agentAudioVolume: elements.agentAudio.volume,
    agentAudioReadyState: elements.agentAudio.readyState,
    remoteAudioReadyState: remoteTrack?.readyState || null,
    remoteAudioMuted: Boolean(remoteTrack?.muted),
    status: elements.agentAudioStatus.textContent,
  };
};

async function startMicMeter(stream) {
  stopMicMeter();
  const [audioTrack] = stream.getAudioTracks();
  if (!audioTrack) return;
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) return;

  state.audioContext = new AudioContextClass();
  const source = state.audioContext.createMediaStreamSource(new MediaStream([audioTrack]));
  state.analyser = state.audioContext.createAnalyser();
  state.analyser.fftSize = 256;
  state.meterData = new Uint8Array(state.analyser.frequencyBinCount);
  source.connect(state.analyser);
  renderMicMeter();
}

function stopMicMeter() {
  if (state.meterAnimation) cancelAnimationFrame(state.meterAnimation);
  state.meterAnimation = null;
  if (state.audioContext) void state.audioContext.close();
  state.audioContext = null;
  state.analyser = null;
  state.meterData = null;
  setMicLevel(0);
}

function renderMicMeter() {
  if (!state.analyser || !state.meterData) return;
  state.analyser.getByteTimeDomainData(state.meterData);
  let sum = 0;
  for (const value of state.meterData) {
    const normalized = (value - 128) / 128;
    sum += normalized * normalized;
  }
  const rms = Math.sqrt(sum / state.meterData.length);
  const level = Math.min(1, rms * 4);
  setMicLevel(level);
  state.meterAnimation = requestAnimationFrame(renderMicMeter);
}

function setMicLevel(level) {
  elements.previewMeter.style.setProperty("--level", level.toFixed(3));
  elements.previewMeter.classList.toggle("active", level > 0.025);
  elements.callMicMeter.style.setProperty("--level", level.toFixed(3));
  elements.callMicMeter.classList.toggle("active", level > 0.025);
  elements.userWave.style.setProperty("--level", level.toFixed(3));
  state.userSpeakingFromMeter = state.connected && level > SPEAKING_LEVEL;
  updateUserSpeaker();
}

function setAgentSpeaking(speaking) {
  state.agentSpeaking = speaking;
  elements.agentSpeaker.classList.toggle("speaking", speaking);
  elements.stageEmpty.classList.toggle("speaking", speaking);
  elements.agentSpeakerText.textContent = speaking ? "Speaking" : "Listening";
}

function startAgentVideoMonitor() {
  if (state.agentVideoCheckTimer) window.clearTimeout(state.agentVideoCheckTimer);
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) return;

  const checkFrame = () => {
    if (!elements.agentVideo.srcObject || !state.pc) return;
    const hasVisibleFrame = hasVisibleVideoFrame(elements.agentVideo, canvas, context);
    elements.stageEmpty.classList.toggle("hidden", hasVisibleFrame);
    elements.agentVideo.classList.toggle("pending", !hasVisibleFrame);
    state.agentVideoCheckTimer = window.setTimeout(checkFrame, 800);
  };

  state.agentVideoCheckTimer = window.setTimeout(checkFrame, 300);
}

function hasVisibleVideoFrame(video, canvas, context) {
  if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || !video.videoWidth || !video.videoHeight) {
    return false;
  }

  const width = 32;
  const height = 18;
  canvas.width = width;
  canvas.height = height;

  try {
    context.drawImage(video, 0, 0, width, height);
    const { data } = context.getImageData(0, 0, width, height);
    let total = 0;
    for (let index = 0; index < data.length; index += 4) {
      total += data[index] + data[index + 1] + data[index + 2];
    }
    return total / (width * height * 3) > 8;
  } catch {
    return true;
  }
}

function updateUserSpeaker() {
  const isSpeaking = (state.userSpeakingFromRTVI || state.userSpeakingFromMeter) && !state.muted;
  elements.userSpeaker.classList.toggle("speaking", isSpeaking);
  elements.userSpeakerText.textContent = state.muted ? "Muted" : isSpeaking ? "Speaking" : "Mic active";
}

function resetMessages(text) {
  state.lastMessageByRole.clear();
  elements.messages.replaceChildren();
  addUniqueMessage("system", text);
}

function addUniqueMessage(role, text) {
  const normalized = text.replace(/\s+/g, " ").trim();
  if (!normalized) return;
  if (state.lastMessageByRole.get(role) === normalized) return;
  state.lastMessageByRole.set(role, normalized);
  addMessage(role, normalized);
}

function addMessage(role, text) {
  const node = document.createElement("div");
  node.className = `message ${role}`;
  node.textContent = text;
  elements.messages.appendChild(node);
  elements.messages.scrollTop = elements.messages.scrollHeight;
}

function setPreviewStatus(text, live) {
  elements.previewStatus.innerHTML = `<span class="status-dot${live ? " live" : ""}"></span><span></span>`;
  elements.previewStatus.lastElementChild.textContent = text;
}

function setStatus(text, live = false) {
  elements.statusText.textContent = text;
  elements.statusDot.classList.toggle("live", live);
}

function setButtons({ connecting = false, connected = false }) {
  elements.muteButton.disabled = !connected;
  elements.hangupButton.disabled = !connected && !connecting;
}
