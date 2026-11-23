self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(clients.claim());
});

// Sunucudan gelen Push bildirimini yakala ve ekrana bas
self.addEventListener("push", (event) => {
  let data = {
    title: "ANC Dosya Takip",
    body: "Yeni bir bildiriminiz var.",
    url: "/",
    pdf_url: ""
  };

  if (event.data) {
    try {
      data = Object.assign(data, event.data.json());
    } catch (e) {
      data.body = event.data.text();
    }
  }

  const options = {
    body: data.body,
    icon: "/static/icon-192.png",
    badge: "/static/icon-192.png",
    vibrate: [200, 100, 200],
    data: {
      url: data.url || "/",
      pdf_url: data.pdf_url || ""
    }
  };

  // Eğer bildirimde PDF linki varsa Android bildirim çubuğuna doğrudan indirme butonu ekle
  if (data.pdf_url) {
    options.actions = [
      { action: "pdf_indir", title: "📄 Resmi PDF'i İndir" }
    ];
  }

  event.waitUntil(
    self.registration.showNotification(data.title, options)
  );
});

// Bildirime veya içindeki "PDF İndir" butonuna tıklandığında
self.addEventListener("notificationclick", (event) => {
  event.notification.close();

  const pdfUrl = event.notification.data.pdf_url;
  const targetUrl = (event.action === "pdf_indir" && pdfUrl) ? pdfUrl : (pdfUrl || event.notification.data.url || "/");

  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      // Dış PDF bağlantısı ise doğrudan yeni sekmede/tarayıcıda indir
      if (targetUrl.startsWith("http")) {
        return clients.openWindow(targetUrl);
      }
      // Uygulama içi sayfa ise açık pencereyi odakla veya aç
      for (const client of clientList) {
        if ("focus" in client) {
          client.navigate(targetUrl);
          return client.focus();
        }
      }
      return clients.openWindow(targetUrl);
    })
  );
});