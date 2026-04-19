function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("Greenly")
    .addItem("Open Chat", "showSidebar")
    .addToUi();

}

function showSidebar() {
  const html = HtmlService.createHtmlOutputFromFile("sidebar")
    .setTitle("Greenly")
    .setWidth(300);
  SpreadsheetApp.getUi().showSidebar(html);
}
