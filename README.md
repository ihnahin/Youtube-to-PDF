# SubtitlePro
**Designed & Developed by Md Ismail Hossain Nahin**

YouTube link বা নিজের Audio/Video ফাইল থেকে Subtitle তৈরি, বাংলা অনুবাদ, এবং PDF / SRT / VTT / TXT / CSV Download।

## চালানোর নিয়ম
```
pip install -r requirements.txt
python app.py
```
Windows-এ `run.bat` ডাবল-ক্লিক করলেই হবে। তারপর ব্রাউজারে খুলুন: **http://localhost:5000**

ফোনে (একই Wi-Fi) খুলতে: `HOST=0.0.0.0 python app.py` (Windows: `set HOST=0.0.0.0`) তারপর ফোনে `http://<PC-এর-IP>:5000`

## নতুন যা যা আছে
- **AI Subtitle**: ভিডিওতে Subtitle না থাকলে "AI Generate" দিয়ে (faster-whisper) নিজেই Subtitle বানায়
- **File Upload**: MP3/WAV/MP4/MKV… ফাইল দিলেও Subtitle বানায় (ফাইল Server-এ থাকে না, কাজ শেষে মুছে যায়)
- **ঠিকঠাক বাংলা অনুবাদ**: বাংলা বাদে অন্য ভাষার লেখা Google Translate দিয়ে বাংলায় যায় (যা আগে থেকেই বাংলা, তা অপরিবর্তিত থাকে)
- Result আসার পরেও অন্য ভাষায় অনুবাদ করা যায়
- Live progress bar + Cancel, ভিডিও Player-এর সাথে Subtitle Sync (ক্লিক করলে সেই সময়ে যায়)
- Subtitle Edit, Search, লেখা বড়/ছোট, Dark/Light Theme
- Export: PDF, SRT, VTT, TXT, CSV, JSON
- Mobile + PC দুটোর জন্যই Responsive

## AI Model পরামর্শ
| Model | গতি | সঠিকতা |
|---|---|---|
| tiny / base | দ্রুত | ইংরেজিতে ভালো |
| small | মাঝারি | **বাংলার জন্য সুপারিশ** |
| medium | ধীর | সেরা (RAM বেশি লাগে) |

প্রথমবার Model ডাউনলোড হয় (ইন্টারনেট লাগবে)। GPU থাকলে নিজে থেকেই ব্যবহার করবে।

## Settings (ঐচ্ছিক)
`MAX_AI_MINUTES` (ডিফল্ট 120), `MAX_UPLOAD_MB` (ডিফল্ট 1024), `HOST`, `PORT`

> নোট: নিজের কম্পিউটারে ব্যবহারের জন্য বানানো। পাবলিক সার্ভারে দিলে Login/Rate-limit যোগ করুন।
