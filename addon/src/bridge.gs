var BACKEND_URL_KEY = "BACKEND_URL";
var SESSION_ID_KEY = "SESSION_ID";
var LLM_MODE_KEY = "LLM_MODE";


setBackendUrl("https://17d8-103-226-5-207.ngrok-free.app")
function setBackendUrl(url) {
  PropertiesService.getScriptProperties().setProperty(BACKEND_URL_KEY, url);
}

//---------------------------------

function getBackendUrl() {
  var url = PropertiesService.getScriptProperties().getProperty(BACKEND_URL_KEY);
  if (!url) throw new Error("Backend URL not configured. Run setBackendUrl() first.");
  return url;
}

function getOAuthToken() {
  return ScriptApp.getOAuthToken();
}


function _backendFetch(endpoint, method, payload) {
  var baseUrl = getBackendUrl();
  var token = getOAuthToken();

  var options = {
    method: method,
    headers: { Authorization: "Bearer " + token },
    muteHttpExceptions: true,
  };

  if (payload !== undefined) {
    options.contentType = "application/json";
    options.payload = JSON.stringify(payload);
  }

  var response = UrlFetchApp.fetch(baseUrl + endpoint, options);
  var statusCode = response.getResponseCode();
  var responseText = response.getContentText();

  if (statusCode >= 400) {
    var errorBody;
    try { errorBody = JSON.parse(responseText); } catch (e) {
      throw new Error("Backend error " + statusCode + ": " + responseText);
    }
    throw new Error(errorBody.error ? errorBody.error.message : "Backend error " + statusCode);
  }

  if (!responseText || statusCode === 204) return {};
  try {
    return JSON.parse(responseText);
  } catch (e) {
    throw new Error("Backend returned non-JSON response: " + responseText.substring(0, 200));
  }
}

function sendToBackend(endpoint, payload) {
  return _backendFetch(endpoint, "post", payload);
}


function sendChatMessage(query, mode, llmMode) {
  try {
    var ss          = SpreadsheetApp.getActiveSpreadsheet();
    var sheet       = ss.getActiveSheet();
    var activeRange = SpreadsheetApp.getActiveRange();

    var payload = {
      spreadsheetId: ss.getId(),
      activeSheet:   sheet.getName(),
      activeSheetId: sheet.getSheetId(),
      activeRange:   activeRange ? activeRange.getA1Notation() : null,
      query:         query,
      mode:          mode,
      effort:        llmMode,
    };

    return sendToBackend("/api/v1/chat", payload);
  } catch (e) {
    var msg = e && e.message ? e.message : String(e);
    if (msg.indexOf("Backend URL not configured") !== -1) {
      throw new Error("Backend not set up yet. Please configure the backend URL.");
    }
    if (msg.indexOf("Backend error") === 0 || msg.indexOf("Backend returned non-JSON") === 0) {
      throw new Error(msg);
    }
    throw new Error("Unable to reach backend: " + msg);
  }
}

