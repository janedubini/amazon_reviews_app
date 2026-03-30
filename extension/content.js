/**
 * Content Script — определяет, находимся ли мы на странице Amazon-продукта,
 * и извлекает ASIN и URL для popup.
 */
(function () {
  const url = window.location.href;

  // Извлечение ASIN из URL
  function extractAsin(url) {
    const patterns = [
      /\/dp\/([A-Z0-9]{10})/i,
      /\/gp\/product\/([A-Z0-9]{10})/i,
      /\/product-reviews\/([A-Z0-9]{10})/i,
    ];
    for (const pat of patterns) {
      const m = url.match(pat);
      if (m) return m[1].toUpperCase();
    }
    return null;
  }

  const asin = extractAsin(url);

  // Отправляем данные popup'у по запросу
  chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.type === "GET_PRODUCT_INFO") {
      sendResponse({
        url: window.location.href,
        asin: asin,
        title: document.title,
        isProductPage: !!asin,
      });
    }
  });
})();
