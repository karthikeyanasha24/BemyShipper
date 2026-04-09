Hi Asha
We have an app already built and now we want to improve the Buy-For-Me experience. The goal is to plug a system where a user pastes a product URL or click the buy for me bottom and we automatically extract product data (title, image, price, variants) and display a clean preview inside the app. We’ll start with Amazon and expand later. This needs to be fast, simple, and reliable. Let me know your approach.

Asha Karthikeyan
6:45 PM
Hello sir, Can be done sir, can I get. More details ans budget do you have in mind sir

CT
Cellou Tounkara
6:56 PM
Hi Asha,



Thanks for your response.



The goal is to build a smart product extraction system for our Buy-For-Me feature. A user will paste a product URL (starting with Amazon), and the system should automatically extract:
- Title
- Main image
- Price
- Variants (size, color, etc.)



Then display a clean preview inside the app.



Important:
- This should work reliably without official APIs (scraping or hybrid approach is fine)
- It must be fast and scalable (we’ll expand to other sites later)
- The solution should be built separately first (test environment), then integrated into our app



Please share:
1. Your technical approach (how you’ll extract the data)
2. Similar work you’ve done (if any)
3. Estimated timeline
4. Your budget for this task



We are comparing a few developers, so we’re looking for the best and most efficient solution.



Thanks

Asha Karthikeyan
10:15 PM
Yes sir it can be done

What is the deadline, and which website?
Can you send me those details

CT
Cellou Tounkara
11:13 PM
Hi Asha,



This is for our live app, already available on Google Play (BeMyShipper).



For now, we are starting with Amazon only.



Deadline: Monday or Tuesday



The goal is to build a working prototype where a user inputs an Amazon product URL and the system returns the product data (title, image, price, variants if possible).



We’ll review it together after.



Thanks

Asha Karthikeyan
11:15 PM
Okay sir, sound good, Ill start working, any update on payment sir?

Friday, Apr 03
Asha Karthikeyan
3:19 AM
Awaiting your response sir, also just letting you know that I have started working

Just incase if you have time, have a look at this document sir
https://docs.google.com/spreadsheets/d/13xWPQERZg7on--ZQpqlqP6vEQtLqe38OsQ1PyqfVuIo/edit?gid=0#gid=0

CT
Cellou Tounkara
3:43 AM
Thanks, Asha. Let’s focus on the work first. Payment will depend on the quality of the demo, and we’ll discuss it together after.

Asha Karthikeyan
Apr 2, 2026 | 11:15 PM
Okay sir, sound good, Ill start working, any update on payment sir?

Asha Karthikeyan
3:43 AM
Okay sir

CT
Cellou Tounkara
3:44 AM
Need access to the doc

Asha Karthikeyan
3:44 AM
Given already sir

CT
Cellou Tounkara
3:46 AM
Empty ?

Asha Karthikeyan
3:47 AM
It s not having any data sir, there only one line in a1

Also it's completely optional just incase only

Asha Karthikeyan
1:37 PM
Ready sir
https://drive.google.com/file/d/1IFvgktYjlBYsfcpR_KbHjBXPuQq95mfI/view?usp=sharing

CT
Cellou Tounkara
1:49 PM
Access?

Asha Karthikeyan
1:49 PM
Given sir

CT
Cellou Tounkara
1:54 PM
???

Don’t get anything

Asha Karthikeyan
1:55 PM
Let me just share the results in video i think you couldn't see properly

Asha Karthikeyan
2:32 PM
{
"success": true,
"asin": "B09G9FPHY6",
"title": "Apple iPad (9th Generation): with A13 Bionic chip, 10.2-inch Retina Display, 64GB, Wi-Fi, 12MP front/8MP Back Camera, Touch ID, All-Day Battery Life – Space Gray",
"price": null,
"currency": "$",
"main_image": "https://m.media-amazon.com/images/I/61NGnpjoRDL.AC_SL1500.jpg",
"variants": [
{
"group": "Style",
"options": [
"WiFi",
"WiFi + Cellular"
]
},
{
"group": "Size",
"options": [
"64 GB",
"256GB"
]
},
{
"group": "Color",
"options": [
"Silver",
"Space Gray"
]
},
{
"group": "Configuration",
"options": [
"With AppleCare+",
"Without AppleCare+"
]
}
],
"rating": "4.8",
"review_count": "(75,699)",
"availability": "This item cannot be shipped to your selected delivery location. Please choose a different delivery location.",
"extraction_method": "http",
"error": null
}

This is just basic Skelton of backend once I get the frontend source I can link it and display in listed format sir

Asha Karthikeyan
4:32 PM
Hello sir

Any update on this?

CT
Cellou Tounkara
5:07 PM
Asha, this looks good 👍 thank you.



I have a few things we need to clarify and improve so we can make this usable end-to-end:
1. Price extraction (very important)



• Currently price is null
• Can we reliably extract the product price for most Amazon links?
• If not always, what fallback can we use?



2. Frontend integration



• Once extraction works, can you confirm how this will be displayed in the app?
• We need:
• title
• image
• price
• variant selection (size, color, etc.)



3. Variant handling



• When user selects a variant (e.g. 64GB + Silver), can we:
• update the correct price?
• make sure we capture the exact variant?



4. Availability message



• We saw “cannot be shipped to your location”
• Since we handle shipping, can we ignore this or remove it from user display?



5. Supported websites



• Right now is this only for Amazon?
• Can this method be extended to other websites later?



6. Reliability



• What % of products do you think this will work for?
• Are there cases where extraction fails?



7. Next step



• Once this is stable, we want to connect it fully to Buy-For-Me flow
• Please confirm what is needed from frontend/backend to complete this

Asha Karthikeyan
6:03 PM
Price for this product is not there so I is null now but I checked it for other which has price it is collecting price so you no need to worry about that sir



Frontend it needs to be you suggestion based on your app is can deploy in web app but for app you have to say as you have the source code sir



Variant dynamic can be done



Yes can be displayed



Yes later can be extended but can't give assurance for all websites sir



Mostly it will work because this now handles two method if one fails it fallback to another method so mostly it will work for sure sirthe app code is needed to implement it sir

CT
Cellou Tounkara
Apr 3, 2026 | 5:07 PM
Asha, this looks good 👍 thank you.



I have a few things we need to clarify and improve so we can make this usable end-to-end:
1. Price extraction (very important)



• Currently price is null
• Can we reliably extract the product price for most Amazon links?
• If not always, what fallback can we use?



2. Frontend integration



• Once extraction works, can you confirm how this will be displayed in the app?
• We need:
• title
• image
• price
• variant selection (size, color, etc.)



3. Variant handling



• When user selects a variant (e.g. 64GB + Silver), can we:
• update the correct price?
• make sure we capture the exact variant?



4. Availability message



• We saw “cannot be shipped to your location”
• Since we handle shipping, can we ignore this or remove it from user display?



5. Supported websites



• Right now is this only for Amazon?
• Can this method be extended to other websites later?



6. Reliability



• What % of products do you think this will work for?
• Are there cases where extraction fails?



7. Next step



• Once this is stable, we want to connect it fully to Buy-For-Me flow
• Please confirm what is needed from frontend/backend to complete this

Show more
CT
Cellou Tounkara
6:25 PM
Hi Asha 👍



The goal is to build and test everything first in a separate testing environment before connecting it to the live app.



Before that, I would like you to:
- Download the BeMyShipper app from Google Play
- Create a test account (use any email)
- Go through the app, especially the Buy-For-Me flow
- Try adding a product and see how everything currently works



This is important because the store inside the app is based on WebView, so I want you to clearly understand the user experience and how the flow works.



If after testing you feel confident that you can handle this properly, then we will:
- Set up a testing environment
- Implement and test the extraction system there



Once everything is stable and working correctly, we will connect it to the app and release an update.



Let me know once you have tested the app and your feedback 👍

Asha Karthikeyan
6:45 PM
Yes sir i have already checked it and yes can do it

I'll make a web app for testing so you will understand it better sir

CT
Cellou Tounkara
7:54 PM
Hi Asha 👍



Great that you reviewed the app, that helps a lot.



For the web app, let’s keep it very simple — this is only for testing, not a full product.
We just need:



- URL input

- Extract button

- Display: title, image, price, variants
No complex UI needed — focus is on accuracy and reliability.
Regarding the goal:
We want to test multiple product sources and make the extraction work for as many as possible.
Not everything needs to work perfectly, but we want strong coverage, especially for important ones like Amazon and similar sites.
Please try different product links and let me know:

- which ones work well

- which ones fail

- and what limitations you see
About the hourly rate:
I see your current rate is $10/hr, but I’d like to understand your flexibility since this is an ongoing but controlled scope project.
Can you share your proposal for this work?
Then we can align on something that works for both sides depending on scope and duration.
Let’s first focus on making the extraction reliable 👍

Asha Karthikeyan
7:56 PM
Noted sir, can you quote a price, because I don't want to overquote or under quote it, so I prefer you to quote a fair price based on the scope, else, I'll give if it doesn't match you can negotiate sir

CT
Cellou Tounkara
8:17 PM
Great 👍



Let’s start with a small test this weekend.



- Rate: $6/hour
- Up to 12–15 hours max
- Goal is to validate the approach



What I want you to do:



1. Download the BeMyShipper app and go through the Buy-For-Me flow
2. Test different product links from the stores inside the app
3. Starton Amazon and Zara first
4. Try to extract:
- title
- image
- price
- variants



Also, build a simple web test tool (one page) to:
- paste URL
- extract data
- display result



By Monday, I want to see:
- working examples
- which sites work well
- which ones don’t
- and your approach to improve coverage



Let’s focus on simplicity, speed, and reliability 👍

Asha Karthikeyan
8:19 PM
Screenshot_20260403_201938_BeMyShipper.jpg 
Screenshot_20260403_201938_BeMyShipper.jpg
There is no amazon here,
Just to clarify I need to develop webapp for amazon for testing right?

Writing logic for each website will take time sir, so amazon done but not frontend and Zara yet to done bith frontend as well as backend

CT
Cellou Tounkara
8:40 PM
Give me a quote and details of what is your approach

Asha Karthikeyan
8:45 PM
Right now web app gor testing Right, is that you are asking sir?

CT
Cellou Tounkara
8:50 PM
Yes 👍



You can use a simple web tool or any method that helps you test properly.



The goal is not the tool itself, but to make sure the extraction works reliably.



You can also use WebView from the app or real product URLs — whatever is best for testing.



Focus on:
- testing Amazon extraction properly
- making sure results are accurate (title, image, price, variants)
- validating with real product links
Then we move to another store of possible
Once we are confident with the results, we will integrate it into the app.



Keep it simple and focus on the final result 👍

Asha Karthikeyan
8:51 PM
Okay sir

Yhe quote you mentioned for testing or for the project itself sir?

CT
Cellou Tounkara
8:55 PM
Testing

Got it 👍



For this testing phase, focus on:



1. Amazon (priority)
2. Zara
3. If possible, one more store (like SHEIN or similar)



The goal is to make this work properly across these stores and get strong, reliable results.



Use whatever method you prefer for testing (web tool, WebView, etc.), just make sure the extraction is accurate (title, image, price, variants).



This is for the testing phase. Once we validate everything, we will move to the next step 👍

Asha Karthikeyan
8:56 PM
Okay sir

CT
Cellou Tounkara
9:30 PM
What’s your best suggestion ?

Asha Karthikeyan
9:32 PM
This looks good, first I'll give this so you will get an idea how it displays the data sir

CT
Cellou Tounkara
Apr 3, 2026 | 8:55 PM
Got it 👍



For this testing phase, focus on:



1. Amazon (priority)
2. Zara
3. If possible, one more store (like SHEIN or similar)



The goal is to make this work properly across these stores and get strong, reliable results.



Use whatever method you prefer for testing (web tool, WebView, etc.), just make sure the extraction is accurate (title, image, price, variants).



This is for the testing phase. Once we validate everything, we will move to the next step 👍

Show more
Saturday, Apr 04
CT
Cellou Tounkara
1:35 AM
Alright

It Will ready for Monday ?

Or before?

Or you don’t work on week-end ?

Asha Karthikeyan
1:37 AM
I do sir, but can give assurance on Monday, based on trial and error only it depends but definitely I'll try to give it as soon as possible

Have you considered this sir?
Van I know what do you think?

Asha Karthikeyan
Apr 3, 2026 | 1:37 PM
Ready sir
https://drive.google.com/file/d/1IFvgktYjlBYsfcpR_KbHjBXPuQq95mfI/view?usp=sharing

Show more
I mean this one

Asha Karthikeyan
Apr 3, 2026 | 3:19 AM
Just incase if you have time, have a look at this document sir
https://docs.google.com/spreadsheets/d/13xWPQERZg7on--ZQpqlqP6vEQtLqe38OsQ1PyqfVuIo/edit?gid=0#gid=0

Show more
CT
Cellou Tounkara
1:39 AM
There is one line in the document . I cant even read

Sounds good . Let me know anytime

Asha Karthikeyan
Apr 4, 2026 | 1:37 AM
I do sir, but can give assurance on Monday, based on trial and error only it depends but definitely I'll try to give it as soon as possible

Show more
Asha Karthikeyan
1:40 AM
Okay sir

You couldn't see the content that is there sir?

CT
Cellou Tounkara
Apr 4, 2026 | 1:39 AM
There is one line in the document . I cant even read

CT
Cellou Tounkara
1:41 AM
Which documents ? Check yourself

Asha Karthikeyan
1:41 AM
Yes sir i can see

That's why I am asking sir

CT
Cellou Tounkara
1:42 AM
Okay tell me what you want directly

Because there one line and don’t know what’s does that mean

Speak directly

Asha Karthikeyan
1:43 AM
Can't here sir

Okay then fine we can continue in this platform itself sir

No issues

I'll start with remaining work sir

CT
Cellou Tounkara
1:44 AM
Ohhh I just saw it now

Let’s start here if everything goes well we can adjust

No worries at all

Asha Karthikeyan
1:44 AM
Okay sir

CT
Cellou Tounkara
2:22 AM
Let me know when it’s done

Asha Karthikeyan
2:22 AM
Sure sir

Asha Karthikeyan
1:17 PM
Here it is sir

2 files 
demo_3.mp4
3 MB
demo_2.mp4
1 MB
CT
Cellou Tounkara
5:30 PM
Cellou Tounkara removed this message

CT
Cellou Tounkara
8:24 PM
This looks really good 👍 I like the progress you’ve made so far.



Since you already downloaded the app, I want you to take a closer look at the real flow.



Please go to the Buy For Me section again and try it with a few products to clearly understand how it works today (user flow, manual input, etc.).



Based on that, I want you to propose a realistic approach that fits directly into our system:
- How your solution would integrate with our WebView flow
- What happens when the user clicks “Buy For Me”
- How the extracted data would be returned and displayed in the app



Please think both backend + frontend, not just extraction.



Also, just to be clear — if we move forward together, you will be responsible for handling the full solution end-to-end:
- extraction system
- backend integration
- frontend updates
- and app updates (Google Play & Apple Store)



The goal is to align everything with our existing system and make it work smoothly.



Looking forward to your approach 👍

Sunday, Apr 05
Asha Karthikeyan
3:39 AM
So instead of user filling you wanted it to be automatically filling right sir?

CT
Cellou Tounkara
3:52 AM
Yes exactly

Asha Karthikeyan
3:53 AM
Okay sir, will you share you source app?
Or I just develop as Api and you will integrate it?

CT
Cellou Tounkara
3:57 AM
Yes exactly — the goal is to have the product details automatically filled when the user clicks “Buy For Me”.



Regarding your question, for the first phase I prefer that you build the extraction system as an API in a separate environment, so we can test and validate everything clearly.



At the same time, the final goal is a full end-to-end solution. If we move forward together, you will handle:
- product extraction system
- backend integration with our existing system
- frontend updates (Buy For Me flow)
- and app updates (Google Play & Apple Store)



Once we agree on the approach, I will provide the necessary access for proper integration and deployment.



For the approach, I would like:
- combine extraction + integration + frontend into one phase
- start with 3 stores: Amazon, Zara, and SHEIN
- then push an update to Google Play and proceed with Apple submission
- after that, scale to more stores



Also, we are currently reviewing a few developer proposals. Your profile is strong and aligned with what we need, but since we are looking for a long-term collaboration, we need a reasonable starting price.



Could you please share:
- your technical approach (backend + frontend)
- timeline
- and a fixed price for this first phase



Looking forward to your proposal 👍

Asha Karthikeyan
3:59 AM
Sure sir, right now I am not in work space I'll get those once I am to Workspace sir

CT
Cellou Tounkara
4:06 AM
No problem at all

Asha Karthikeyan
7:40 AM
After I create this as an API can you create contract sir?

CT
Cellou Tounkara
8:06 AM
Don’t create anything yet . Just make your proposal please

This first please

CT
Cellou Tounkara
Apr 5, 2026 | 3:57 AM
Yes exactly — the goal is to have the product details automatically filled when the user clicks “Buy For Me”.



Regarding your question, for the first phase I prefer that you build the extraction system as an API in a separate environment, so we can test and validate everything clearly.



At the same time, the final goal is a full end-to-end solution. If we move forward together, you will handle:
- product extraction system
- backend integration with our existing system
- frontend updates (Buy For Me flow)
- and app updates (Google Play & Apple Store)



Once we agree on the approach, I will provide the necessary access for proper integration and deployment.



For the approach, I would like:
- combine extraction + integration + frontend into one phase
- start with 3 stores: Amazon, Zara, and SHEIN
- then push an update to Google Play and proceed with Apple submission
- after that, scale to more stores



Also, we are currently reviewing a few developer proposals. Your profile is strong and aligned with what we need, but since we are looking for a long-term collaboration, we need a reasonable starting price.



Could you please share:
- your technical approach (backend + frontend)
- timeline
- and a fixed price for this first phase



Looking forward to your proposal 👍

Show more
Asha Karthikeyan
8:07 AM
Okay sir

Asha Karthikeyan
8:14 AM
Here is my proposed approach for the first phase:



1. Technical Approach



Backend (Core Extraction API):



Build a dedicated API that takes a product URL and returns:
title
image
price
variants (with mapping to price)
Use multi-layer extraction:
Primary: structured data (JSON-LD / meta tags)
Secondary: DOM parsing
Fallback: alternate selectors / pattern-based extraction
Add retry + fallback logic to improve reliability



Variant Handling:



Capture all available variants (size, color, etc.)
Map each variant to its corresponding price (if available)
Return structured variant data so frontend can dynamically update



Frontend (Test + Integration Ready):



Simple test UI (web):
URL input
Extract button
Display: title, image, price, variants
For app integration:
API will return clean JSON
WebView or app can consume and auto-fill Buy-For-Me fields



Integration with App Flow:



When user clicks “Buy For Me”:
URL is passed to API
API returns product data
Fields are auto-filled (instead of manual input)
Availability messages (like shipping restriction) can be ignored/filtered at API level



Supported Stores (Phase 1):



Amazon (priority, high reliability)
Zara
SHEIN (or similar)



2. Reliability Expectation



Amazon: ~85–95% success (with fallback methods)
Zara/SHEIN: ~70–85% initially
Fail cases:
dynamic JS-heavy pages
region-restricted content
These can be improved iteratively



3. Timeline



Day 1–2: Amazon extraction (stable)
Day 3: Zara
Day 4: SHEIN
Day 5: Testing + improvements + report



4. Fixed Price (First Phase)



$120 – $150 total
(based on ~12–15 hours as discussed)



5. Next Step



Deliver working API + test UI
Show:
working links
failed cases
improvement plan
Then proceed to full integration (backend + app + deployment)



Let me know if this aligns with your expectations

CT
Cellou Tounkara
5:37 PM
Thank you, this is clear and I like your structured approach 👍



Just to confirm, the $120–150 is only for the API + test UI phase, correct?



For me, the main goal is the full solution integrated into our app (backend + frontend + app updates).



So before moving forward, I would like to understand:
- what would be the total cost for the complete integration (API + backend + frontend + Google Play updates& Apple submission )
- and the full timeline for everything



I prefer to work with a clear full-scope plan from the beginning.



Let me know 👍

Asha Karthikeyan
5:48 PM
Yes, right — the $120–150 is only for the API + test UI phase.



For the complete solution (API + backend + frontend + app updates + store submissions), here is the full scope:



Total Cost: $350



Breakdown:



- API development + testing: $120
- Backend integration: $80
- Frontend integration (UI + app connection): $100
- Google Play update + Apple App Store submission: $50



Timeline:



- API + test UI: 2–3 days
- Backend + frontend integration: 5 - 7 days
- App updates + submissions: 3-4 days



This covers a complete working solution end-to-end.



Also the timeline I mentioned is max but I'll complete it before time

Monday, Apr 06
Asha Karthikeyan
9:16 AM
Hello sir

any update?

or should I need to hold?

Asha Karthikeyan
3:02 PM
Hello sir

CT
Cellou Tounkara sent an offer

4:57 PM
We are improving the Buy For Me experience in our mobile app (BeMyShipper).



The objective is to build a scalable product extraction system where a user submits a product URL and the system automatically extracts structured product data (title, image, price, variants, product URL) and integrates it seamlessly into the app.



We will start with Amazon ,Zara, SHEIN.



The system must be accurate, robust, and designed to scale across multiple e-commerce platforms. Requirements & Ownership



All work must be delivered under BeMyShipper infrastructure:
• Code must be pushed to BeMyShipper GitHub organization
• No personal accounts for deployment, servers, or access
• Full documentation required (setup, deployment, architecture)
• System must be fully transferable, independent, and scalable

Est. Budget: $380.00

Milestone 1: API development + testing. Amazon, SHEIN,Zara extraction (stable) • Output: structured (title, image, price, variants, product URL) • Test UI / validation • Real product testing + edge case

Project funds: $150.00

View offer
Made an offer Asha

Asha Karthikeyan
4:58 PM
Okay sir

CT
Cellou Tounkara
5:00 PM
If everything is perfect . I’ll gave 100$ bonus at the end . Let’s try to have the most perfect extraction possible

Asha Karthikeyan
5:05 PM
Okay sir, I'll give my best even without bonus sir, so I'll give my best sir

CT
Cellou Tounkara
5:07 PM
Sound good . Thank you



