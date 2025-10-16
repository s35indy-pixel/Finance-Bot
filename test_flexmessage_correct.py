#!/usr/bin/env python3
import json
from linebot.v3.messaging import ReplyMessageRequest, FlexMessage
from flex_ui import build_query_menu_bubble

def test_flexmessage_correct():
    """測試正確的 FlexMessage 用法"""
    
    print("=== 測試 FlexMessage 物件 ===")
    
    # 建立 bubble
    bubble = build_query_menu_bubble()
    print("Bubble 內容:")
    print(json.dumps(bubble, indent=2, ensure_ascii=False))
    
    # 建立 FlexMessage 物件
    try:
        flex_message = FlexMessage(altText="請選擇查詢範圍", contents=bubble)
        print(f"\n✅ FlexMessage 物件創建成功")
        print(f"altText: {flex_message.alt_text}")
        print(f"contents type: {type(flex_message.contents)}")
        
        # 檢查 FlexMessage 的 to_dict
        flex_dict = flex_message.to_dict()
        print(f"\nFlexMessage.to_dict():")
        print(json.dumps(flex_dict, indent=2, ensure_ascii=False))
        
        # 建立 ReplyMessageRequest
        reply_request = ReplyMessageRequest(
            replyToken="test_token",
            messages=[flex_message]
        )
        print(f"\n✅ ReplyMessageRequest 創建成功")
        
        # 序列化 ReplyMessageRequest
        reply_dict = reply_request.to_dict()
        print(f"\nReplyMessageRequest.to_dict():")
        print(json.dumps(reply_dict, indent=2, ensure_ascii=False))
        
        # 檢查 messages[0] 的內容
        if "messages" in reply_dict and len(reply_dict["messages"]) > 0:
            first_message = reply_dict["messages"][0]
            print(f"\n第一個訊息的詳細內容:")
            print(json.dumps(first_message, indent=2, ensure_ascii=False))
            
            if not first_message.get('altText'):
                print("❌ 問題：altText 是空的！")
            if not first_message.get('contents'):
                print("❌ 問題：contents 是空的！")
        else:
            print("❌ 問題：沒有找到 messages")
            
    except Exception as e:
        print(f"❌ 測試失敗: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_flexmessage_correct()

