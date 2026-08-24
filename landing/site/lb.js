// Лайтбокс и кнопка «копировать» — ровно два действия, ради которых незачем
// тянуть библиотеку.
(function () {
  var lb = document.getElementById('lb');
  var img = lb && lb.querySelector('img');

  document.querySelectorAll('[data-lb]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      e.preventDefault();
      img.src = a.getAttribute('href');
      lb.classList.add('on');
      document.body.style.overflow = 'hidden';
    });
  });

  function close() {
    lb.classList.remove('on');
    document.body.style.overflow = '';
    // src чистим с задержкой: иначе на закрытии мелькает пустая рамка
    setTimeout(function () { if (!lb.classList.contains('on')) img.src = ''; }, 200);
  }
  if (lb) lb.addEventListener('click', close);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && lb.classList.contains('on')) close();
  });

  document.querySelectorAll('[data-copy]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var cmd = btn.parentNode.textContent.replace(btn.textContent, '').trim();
      cmd = cmd.replace(/^\$\s*/, '');
      navigator.clipboard.writeText(cmd).then(function () {
        var was = btn.textContent;
        btn.textContent = 'скопировано';
        setTimeout(function () { btn.textContent = was; }, 1600);
      });
    });
  });
})();
