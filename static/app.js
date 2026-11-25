document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll('input[type="file"]').forEach(input => {
    input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      if (file && file.size > 5 * 1024 * 1024) {
        alert("Please choose an image smaller than 5 MB.");
        input.value = "";
      }
    });
  });
  document.querySelectorAll('input[type="date"]').forEach(input => {
    if (!input.max) input.max = new Date().toISOString().slice(0, 10);
  });
});
