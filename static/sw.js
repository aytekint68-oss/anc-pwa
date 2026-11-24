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
    tag: "anc-bildirim-" + Date.now(),
    renotify: true,
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

  const notifData = event.notification.data || {};
  const pdfUrl = notifData.pdf_url;
  const targetUrl = (event.action === "pdf_indir" && pdfUrl)
    ? pdfUrl
    : (pdfUrl || notifData.url || "/");

  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      // Dış PDF bağlantısı ise doğrudan yeni sekmede/tarayıcıda aç
      if (targetUrl.startsWith("http")) {
        return clients.openWindow(targetUrl);
      }

      // Uygulama içi sayfa ise açık pencereyi güvenle yönlendir ve odakla
      for (const client of clientList) {
        if ("navigate" in client && "focus" in client) {
          return client.navigate(targetUrl)
            .then((navigatedClient) => (navigatedClient ? navigatedClient.focus() : clients.openWindow(targetUrl)))
            .catch(() => clients.openWindow(targetUrl));
        }
      }

      return clients.openWindow(targetUrl);
    })
  );
});