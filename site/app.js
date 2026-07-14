const state={items:[]};const $=s=>document.querySelector(s);const money=n=>`$${Number(n||0).toFixed(2)}`;
async function load(){const res=await fetch('data/items.json',{cache:'no-store'});const data=await res.json();state.items=data.items||[];$('#updated').textContent=`업데이트: ${data.updated_at}`;render()}
function render(){
 const q=$('#search').value.trim().toLowerCase(),format=$('#format').value,size=$('#size').value,sort=$('#sort').value;
 let items=state.items.filter(i=>{const text=`${i.title} ${i.tag} ${i.category}`.toLowerCase();return(!q||text.includes(q))&&(!format||i.format===format)&&(!size||i.size===size)});
 items.sort((a,b)=>sort==='total'?(a.price+a.shipping)-(b.price+b.shipping):(b.score||0)-(a.score||0));
 $('#count').textContent=items.length;const grid=$('#grid');grid.innerHTML='';
 for(const item of items){const node=$('#card').content.cloneNode(true);node.querySelector('.thumb').src=item.image;node.querySelector('.thumb').alt=item.title;
 node.querySelector('h2').textContent=item.title;node.querySelector('.format').textContent=item.format==='AUCTION'?'경매':'즉시구매';
 node.querySelector('.score').textContent=`추천 ${item.score??'-'}점`;node.querySelector('.price').textContent=money(item.price);
 node.querySelector('.shipping').textContent=item.shipping?money(item.shipping):'무료';node.querySelector('.total').textContent=money(item.price+item.shipping);
 node.querySelector('.tag-size').textContent=`${item.tag||'미확인'} / ${item.size||'미확인'}`;node.querySelector('.risk').textContent=item.risk||'';
 const ebay=node.querySelector('.ebay');ebay.href=item.ebay_url||'#';if(!item.ebay_url||item.ebay_url==='#')ebay.classList.add('disabled');
 const defunkd=node.querySelector('.defunkd');defunkd.href=item.defunkd_url||'#';if(!item.defunkd_url)defunkd.classList.add('disabled');grid.appendChild(node)}
}
['search','format','size','sort'].forEach(id=>$('#'+id).addEventListener('input',render));
load().catch(err=>{$('#updated').textContent=`데이터 오류: ${err.message}`});
